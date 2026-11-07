"""Durable, reference-only notifications with authorization at projection and read.

One inbox row groups a recipient's thread activity. An independent projection
version preserves unread activity when jobs finish out of event order. Workers
claim without holding authority locks, commit, then use user -> share -> job
lock order. Projection and completion commit together; stale leases do nothing.
"""

import json
import threading
import time
import uuid
from datetime import timedelta, timezone

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, case, or_, select, text

from app.api import deps
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_sessionmaker
from app.schemas import discussions as s
from app.services import base_discussions, collaboration_auth, managed_service
from app.services import discussion_service as discussions

LEASE_TTL = timedelta(seconds=60)


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_inbox_enabled
    )


def require_enabled():
    if not enabled():
        discussions.error(404, "inbox_preview_disabled")


def following(db, user_id, thread_id, preference=None):
    if preference is not None:
        return preference.following
    # Participation follows by default, including discussions predating inbox
    # deployment. An explicit unsubscribe always overrides this default.
    return (
        db.scalar(
            select(m.DiscussionMessage.id)
            .where(
                m.DiscussionMessage.thread_id == thread_id,
                m.DiscussionMessage.author_id == user_id,
            )
            .limit(1)
        )
        is not None
    )


def enqueue(db, ctx, command, thread, message, sequence):
    """Called inside the comment transaction, after its immutable revision exists."""
    if not enabled() or not isinstance(command, (s.ThreadCreate, s.ReplyCreate, s.MessageEdit)):
        return
    mentions = command.body.mentioned_users()
    if isinstance(command, s.MessageEdit):
        previous = db.get(m.DiscussionMessageRevision, (message.id, message.revision - 1))
        if previous and previous.body:
            mentions -= s.MessageBody.model_validate(previous.body).mentioned_users()
    preferences = {
        row.user_id: row
        for row in db.scalars(
            select(m.DiscussionSubscription).where(m.DiscussionSubscription.thread_id == thread.id)
        )
    }
    recipients = {identity: "mention" for identity in mentions}
    if isinstance(command, s.ReplyCreate):
        participants = set(
            db.scalars(
                select(m.DiscussionMessage.author_id)
                .where(
                    m.DiscussionMessage.thread_id == thread.id,
                    m.DiscussionMessage.author_id.is_not(None),
                )
                .distinct()
            )
        )
        participants.update(identity for identity, pref in preferences.items() if pref.following)
        for identity in participants:
            pref = preferences.get(identity)
            if pref is None or pref.following:
                recipients.setdefault(identity, "reply")
    for identity, reason in recipients.items():
        pref = preferences.get(identity)
        if identity == ctx.user.id or pref is not None and pref.muted:
            continue
        db.add(
            m.DiscussionNotification(
                share_id=ctx.share.id,
                sequence=sequence,
                user_id=identity,
                thread_id=thread.id,
                message_id=message.id,
                reason=reason,
            )
        )


def member(db, share, user):
    if user.is_admin or share.owner_user_id == user.id:
        return True
    return (
        db.scalar(
            select(m.ShareMember.id)
            .where(
                m.ShareMember.share_id == share.id,
                m.ShareMember.user_id == user.id,
            )
            .with_for_update(read=True)
        )
        is not None
    )


def readable_context(db, share, user, thread, *, scope=None, entry=None, rules=None):
    if thread is None or thread.deleted:
        return None
    scope = scope if scope is not None else db.get(m.DiscussionScope, thread.scope_id)
    if scope is None or scope.share_id != share.id:
        return None
    entry = entry if entry is not None else db.get(m.ManagedEntry, scope.entry_id)
    if (
        entry is None
        or entry.deleted
        or entry.share_id != share.id
        or entry.record.get("type") not in {"markdown", "image", "file"}
    ):
        return None
    ctx = discussions.Context(user, share, entry, user.is_admin or share.owner_user_id == user.id)
    if scope.kind != discussions.scope_kind(ctx) or managed_service.permission(
        share,
        user,
        entry.path,
        rules if rules is not None else managed_service.rules_for(db, share, user),
    ) not in {"read", "write"}:
        return None
    return ctx if base_discussions.can_read_thread(db, ctx, thread) else None


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def worker_limits(db):
    if db.get_bind().dialect.name == "postgresql":
        # A busy permission writer must not hold application shutdown forever.
        # These limits are transaction-local and never change request sessions.
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '10s'"))


def claim(factory):
    if not enabled():
        return None
    now = security.utcnow()
    with factory.begin() as db:
        worker_limits(db)
        job = db.scalar(
            select(m.DiscussionNotification)
            .where(
                m.DiscussionNotification.completed_at.is_(None),
                or_(
                    m.DiscussionNotification.lease_until.is_(None),
                    m.DiscussionNotification.lease_until <= now,
                ),
            )
            .order_by(
                m.DiscussionNotification.sequence,
                m.DiscussionNotification.share_id,
                m.DiscussionNotification.user_id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.lease_token, job.lease_until = uuid.uuid4(), now + LEASE_TTL
        return job.share_id, job.sequence, job.user_id, job.lease_token


def project(factory, claimed):
    if not enabled():
        return False
    share_id, sequence, user_id, lease = claimed
    with factory.begin() as db:
        worker_limits(db)
        user = db.scalar(select(m.User).where(m.User.id == user_id).with_for_update(read=True))
        share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
        authorized = bool(
            user and user.is_active and share and share.managed and member(db, share, user)
        )
        job = db.scalar(
            select(m.DiscussionNotification)
            .where(
                m.DiscussionNotification.share_id == share_id,
                m.DiscussionNotification.sequence == sequence,
                m.DiscussionNotification.user_id == user_id,
            )
            .with_for_update()
        )
        if (
            job is None
            or job.completed_at is not None
            or job.lease_token != lease
            or job.lease_until is None
            or aware(job.lease_until) <= security.utcnow()
            or not enabled()
        ):
            return False
        thread = db.get(m.DiscussionThread, job.thread_id)
        message = db.get(m.DiscussionMessage, job.message_id)
        pref = db.get(m.DiscussionSubscription, (user_id, job.thread_id))
        deliver = (
            authorized
            and readable_context(db, share, user, thread) is not None
            and message is not None
            and not message.deleted
            and message.thread_id == thread.id
            and message.author_id != user_id
            and not (pref and pref.muted)
        )
        if deliver and job.reason == "reply":
            deliver = following(db, user_id, thread.id, pref)
        if deliver and job.reason == "mention":
            revision = db.get(m.DiscussionMessageRevision, (message.id, message.revision))
            deliver = bool(
                revision
                and revision.body
                and user_id in s.MessageBody.model_validate(revision.body).mentioned_users()
            )
            if (
                not deliver
                and message.created_sequence == sequence
                and thread.created_sequence < sequence
                and following(db, user_id, thread.id, pref)
            ):
                # Correcting a mention does not cancel the subscribed reply
                # that this same new message would otherwise have delivered.
                job.reason, deliver = "reply", True
        if deliver:
            item = db.get(m.DiscussionInboxItem, (user_id, thread.id))
            if item is None:
                db.add(
                    m.DiscussionInboxItem(
                        user_id=user_id,
                        thread_id=thread.id,
                        share_id=share_id,
                        last_sequence=sequence,
                        activity_at=job.created_at,
                        message_id=message.id,
                        reason=job.reason,
                    )
                )
            else:
                item.version += 1
                if sequence > item.last_sequence:
                    item.activity_at = job.created_at
                    item.last_sequence, item.message_id, item.reason = (
                        sequence,
                        message.id,
                        job.reason,
                    )
        job.delivered = bool(deliver)
        job.completed_at = security.utcnow()
        job.lease_token, job.lease_until = None, None
        return bool(deliver)


def run_batch(stop=None, *, factory=None, limit=100):
    stop = stop or threading.Event()
    factory = factory or get_sessionmaker()
    deadline, processed = time.monotonic() + 5, 0
    while processed < min(max(limit, 0), 100) and not stop.is_set() and time.monotonic() < deadline:
        claimed = claim(factory)
        if claimed is None:
            break
        if stop.is_set():
            break  # Lease expires; no notification is discarded.
        project(factory, claimed)
        processed += 1
    return processed


def preference_data(db, user_id, thread_id):
    pref = db.get(m.DiscussionSubscription, (user_id, thread_id))
    return {
        "version": pref.version if pref else 0,
        "following": following(db, user_id, thread_id, pref),
        "muted": bool(pref and pref.muted),
    }


def subscription(db, token, share_id, entry_id, thread_id, update=None):
    require_enabled()
    ctx = discussions.context(
        db, token, share_id, entry_id, write=update is not None, require_comment=False
    )
    thread = discussions.thread_for(db, ctx, thread_id)
    if update is not None:
        pref = db.get(m.DiscussionSubscription, (ctx.user.id, thread.id))
        version = pref.version if pref else 0
        # An exact lost-response retry may observe its accepted successor.
        if (
            pref
            and version == update.expected_version + 1
            and pref.following == update.following
            and pref.muted == update.muted
        ):
            result = preference_data(db, ctx.user.id, thread.id)
            db.commit()
            return result
        if version != update.expected_version:
            discussions.error(409, "stale_subscription_version")
        if pref is None:
            pref = m.DiscussionSubscription(user_id=ctx.user.id, thread_id=thread.id)
            db.add(pref)
        pref.version, pref.following, pref.muted = version + 1, update.following, update.muted
        db.flush()
    result = preference_data(db, ctx.user.id, thread.id)
    db.commit()
    return result


def item_state(item):
    return {
        "version": item.version,
        "read_version": item.read_version,
        "dismissed_version": item.dismissed_version,
        "unread": item.read_version < item.version,
    }


def update_item(db, token, share_id, entry_id, thread_id, update):
    require_enabled()
    ctx = discussions.context(db, token, share_id, entry_id, write=True, require_comment=False)
    thread = discussions.thread_for(db, ctx, thread_id)
    item = db.get(m.DiscussionInboxItem, (ctx.user.id, thread.id))
    if item is None or item.share_id != ctx.share.id:
        discussions.error(404, "target_unavailable")
    if update.seen_version > item.version:
        discussions.error(409, "inbox_version_unavailable")
    if update.action == "unread":
        if update.seen_version != item.version:
            discussions.error(409, "stale_inbox_version")
        item.read_version = min(item.read_version, update.seen_version - 1)
        item.dismissed_version = min(item.dismissed_version, update.seen_version - 1)
    else:
        item.read_version = max(item.read_version, update.seen_version)
        if update.action == "dismiss":
            item.dismissed_version = max(item.dismissed_version, update.seen_version)
    result = item_state(item)
    db.commit()
    return result


def list_items(
    db,
    token,
    share_id,
    *,
    cursor=None,
    limit=50,
    unread_only=False,
    include_muted=False,
    include_dismissed=False,
):
    require_enabled()
    discussions.require_clean_session(db)
    db.expire_all()
    user = collaboration_auth.lock_actor(db, token, require_session=True)
    share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update(read=True))
    if not share or not share.managed or not member(db, share, user):
        discussions.error(404, "target_unavailable")
    deps._get_user_from_token(db, token)
    filters = [unread_only, include_muted, include_dismissed]
    identity = ["inbox-v1", str(user.id), str(share.id), filters]
    before = None
    if cursor:
        try:
            value = json.loads(discussions.cursor_codec().decrypt(cursor.encode(), ttl=86400))
            if (
                value[:4] != identity
                or len(value) != 5
                or type(value[4]) is not int
                or value[4] < 1
            ):
                raise ValueError
            before = value[4]
        except (InvalidToken, ValueError, TypeError, UnicodeError, IndexError):
            discussions.error(409, "inbox_cursor_reset")
    # The share read lock above excludes inventory/rule writers. These rules
    # belong only to this request, never a cross-request authorization cache.
    rules = managed_service.rules_for(db, share, user)
    participated = (
        select(m.DiscussionMessage.id)
        .where(
            m.DiscussionMessage.thread_id == m.DiscussionThread.id,
            m.DiscussionMessage.author_id == user.id,
        )
        .correlate(m.DiscussionThread)
        .exists()
    )
    follows = case(
        (m.DiscussionSubscription.thread_id.is_not(None), m.DiscussionSubscription.following),
        else_=participated,
    )
    query = (
        select(
            m.DiscussionInboxItem,
            m.DiscussionThread,
            m.DiscussionScope,
            m.ManagedEntry,
            m.DiscussionMessage,
            m.DiscussionSubscription,
            follows,
        )
        .select_from(m.DiscussionInboxItem)
        .join(m.DiscussionThread, m.DiscussionThread.id == m.DiscussionInboxItem.thread_id)
        .join(m.DiscussionScope, m.DiscussionScope.id == m.DiscussionThread.scope_id)
        .join(m.ManagedEntry, m.ManagedEntry.id == m.DiscussionScope.entry_id)
        .outerjoin(m.DiscussionMessage, m.DiscussionMessage.id == m.DiscussionInboxItem.message_id)
        .outerjoin(
            m.DiscussionSubscription,
            and_(
                m.DiscussionSubscription.user_id == user.id,
                m.DiscussionSubscription.thread_id == m.DiscussionThread.id,
            ),
        )
        .where(m.DiscussionInboxItem.user_id == user.id, m.DiscussionInboxItem.share_id == share.id)
    )
    if before is not None:
        query = query.where(m.DiscussionInboxItem.last_sequence < before)
    if unread_only:
        query = query.where(m.DiscussionInboxItem.read_version < m.DiscussionInboxItem.version)
    if not include_dismissed:
        query = query.where(m.DiscussionInboxItem.dismissed_version < m.DiscussionInboxItem.version)
    items = []
    # At most one thread entry, with current content only. Denied/deleted targets
    # contribute neither visible rows nor counts. Pagination is activity based;
    # a client refreshes the first page after draining older pages.
    has_more = False
    with db.execute(
        query.order_by(m.DiscussionInboxItem.last_sequence.desc()).execution_options(yield_per=100)
    ) as rows:
        for item, thread, scope, entry, message, subscription, follows in rows:
            ctx = readable_context(db, share, user, thread, scope=scope, entry=entry, rules=rules)
            if ctx is None:
                continue
            pref = {
                "version": subscription.version if subscription else 0,
                "following": bool(follows),
                "muted": bool(subscription and subscription.muted),
            }
            if pref["muted"] and not include_muted:
                continue
            reason = item.reason
            if not message or message.deleted or message.thread_id != thread.id:
                # Removing the newest message must not erase older discussion
                # activity. Only previously delivered candidates can supply it.
                fallback = db.execute(
                    select(m.DiscussionMessage, m.DiscussionNotification.reason)
                    .join(
                        m.DiscussionNotification,
                        m.DiscussionNotification.message_id == m.DiscussionMessage.id,
                    )
                    .where(
                        m.DiscussionNotification.thread_id == thread.id,
                        m.DiscussionNotification.user_id == user.id,
                        m.DiscussionNotification.share_id == share.id,
                        m.DiscussionNotification.delivered.is_(True),
                        m.DiscussionMessage.thread_id == thread.id,
                        m.DiscussionMessage.deleted.is_(False),
                    )
                    .order_by(m.DiscussionNotification.sequence.desc())
                    .limit(1)
                ).first()
                if fallback is None:
                    continue
                message, reason = fallback
            if len(items) == limit:
                has_more = True
                break
            # No copied snippet: use the current, already-authorized message and
            # scope identifiers. The client fetches its body through the normal API.
            items.append(
                {
                    "thread_id": str(thread.id),
                    "entry_id": ctx.entry.id,
                    "scope_id": str(thread.scope_id),
                    "kind": discussions.scope_kind(ctx),
                    "path": ctx.entry.path,
                    "message_id": str(message.id),
                    "message_revision": message.revision,
                    "reason": reason,
                    "last_sequence": item.last_sequence,
                    "activity_at": aware(item.activity_at),
                    "view_id": str(base_discussions.authorize_thread(db, ctx, thread).view_id)
                    if ctx.entry.record.get("type") == "file"
                    else None,
                    "subscription": pref,
                    **item_state(item),
                }
            )
    next_cursor = (
        discussions.cursor_codec()
        .encrypt(json.dumps([*identity, items[-1]["last_sequence"]]).encode())
        .decode()
        if has_more
        else None
    )
    db.commit()
    return {"items": items, "next_cursor": next_cursor}

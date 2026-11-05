"""Transactional whole-note discussion preview; no network calls while locks are held.

Lock order: actor user (SHARE), actor session (SHARE), share (UPDATE for
commands/SHARE for reads), membership (SHARE), then thread/message. Existing
managed rule/inventory writers lock the share. User/session/membership changes
conflict with the corresponding row lock. A receipt never bypasses current auth.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api import deps
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as s
from app.services import collaboration_auth, collaboration_policy, managed_service


def error(status: int, code: str):
    raise HTTPException(status, {"code": code})


@dataclass
class Context:
    user: m.User
    share: m.Share
    entry: m.ManagedEntry
    moderate: bool


def require_clean_session(db: Session):
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Discussion entry points require a session without pending writes")


def context(
    db: Session, token: str, share_id: uuid.UUID, entry_id: uuid.UUID, *, write=False
) -> Context:
    # These entry points own their transaction; refresh previously read ORM
    # identities, including when tests exercise a stale session intentionally.
    require_clean_session(db)
    db.expire_all()
    user = collaboration_auth.lock_actor(db, token, require_session=True)
    share = db.scalar(
        select(m.Share)
        .where(m.Share.id == share_id)
        .with_for_update(read=not write)
        .execution_options(populate_existing=True)
    )
    if not share or not share.managed:
        error(404, "target_unavailable")
    moderate = user.is_admin or share.owner_user_id == user.id
    if not moderate:
        member = db.scalar(
            select(m.ShareMember)
            .where(m.ShareMember.share_id == share.id, m.ShareMember.user_id == user.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if member is None:
            error(404, "target_unavailable")
    # Recheck expiration after any lock wait; user/session rows remain guarded.
    deps._get_user_from_token(db, token)
    entry = db.scalar(
        select(m.ManagedEntry)
        .where(
            m.ManagedEntry.id == str(entry_id),
            m.ManagedEntry.share_id == share.id,
            m.ManagedEntry.deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if not entry or entry.record.get("type") != "markdown":
        error(404, "target_unavailable")
    permission = managed_service.permission(
        share, user, entry.path, managed_service.rules_for(db, share, user)
    )
    if permission not in {"read", "write"}:
        error(404, "target_unavailable")
    if write and not collaboration_policy.can_comment(
        share, user, entry.path, permission, collaboration_policy.rules_for(db, share, user)
    ):
        error(403, "comment_forbidden")
    return Context(user, share, entry, moderate)


def scope_for(db: Session, ctx: Context) -> m.DiscussionScope | None:
    return db.scalar(
        select(m.DiscussionScope).where(
            m.DiscussionScope.share_id == ctx.share.id, m.DiscussionScope.entry_id == ctx.entry.id
        )
    )


def thread_for(
    db: Session, scope: m.DiscussionScope | None, thread_id: uuid.UUID, *, include_removed=False
) -> m.DiscussionThread:
    if scope is None:
        error(404, "target_unavailable")
    thread = db.scalar(
        select(m.DiscussionThread)
        .where(m.DiscussionThread.id == thread_id, m.DiscussionThread.scope_id == scope.id)
        .execution_options(populate_existing=True)
    )
    if thread is None or (thread.deleted and not include_removed):
        error(404, "target_unavailable")
    return thread


def validate_mentions(db: Session, ctx: Context, body: s.MessageBody):
    mentioned = body.mentioned_users()
    if len(mentioned) > 50:
        error(422, "too_many_mentions")
    for user_id in mentioned:
        user = db.get(m.User, user_id)
        member = user_id == ctx.share.owner_user_id or db.scalar(
            select(m.ShareMember.id).where(
                m.ShareMember.share_id == ctx.share.id, m.ShareMember.user_id == user_id
            )
        )
        if (
            not user
            or not user.is_active
            or not member
            or managed_service.permission(
                ctx.share, user, ctx.entry.path, managed_service.rules_for(db, ctx.share, user)
            )
            not in {"read", "write"}
        ):
            error(422, "mention_unavailable")
    # Delivery will reauthorize recipients. Persist IDs, never a trusted client
    # display name, and never grant access as a side effect of a mention.


def state_precondition(thread, expected):
    if thread.state_revision != expected:
        error(409, "stale_thread_revision")


def apply_command(db: Session, token: str, envelope: s.CommandEnvelope) -> dict:
    # Do not roll back or expire a caller's unrelated pending changes.
    require_clean_session(db)
    try:
        return _apply_command(db, token, envelope)
    except IntegrityError:
        db.rollback()
        error(409, "identifier_unavailable")
    except OperationalError:
        db.rollback()
        # Never pass SQL exceptions containing bound message bodies to the
        # application's generic exception logger.
        error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        error(500, "discussion_storage_error")
    except Exception:
        db.rollback()
        raise


def _apply_command(db: Session, token: str, envelope: s.CommandEnvelope) -> dict:
    request_hash = hashlib.sha256(envelope.canonical_json().encode("utf-8")).hexdigest()
    ctx = context(db, token, envelope.share_id, envelope.entry_id, write=True)
    scope = scope_for(db, ctx)
    receipt = db.get(m.DiscussionReceipt, (ctx.user.id, envelope.operation_id))
    if receipt:
        if receipt.request_hash != request_hash or not scope or receipt.scope_id != scope.id:
            error(409, "operation_id_conflict")
        result = dict(receipt.result)
        db.commit()
        return result
    command = envelope.command
    if hasattr(command, "body"):
        validate_mentions(db, ctx, command.body)
    if isinstance(command, s.ThreadCreate):
        if scope is None:
            scope = m.DiscussionScope(share_id=ctx.share.id, entry_id=ctx.entry.id, kind="note")
            db.add(scope)
            db.flush()
        if db.get(m.DiscussionThread, command.thread_id) is not None:
            error(409, "identifier_unavailable")
    else:
        thread = thread_for(db, scope, command.thread_id)
    # A row-locked counter produces commit order. No sequence allocation or
    # event is committed when any later validation fails.
    ctx.share.collaboration_epoch = ctx.share.collaboration_epoch or uuid.uuid4()
    ctx.share.collaboration_sequence += 1
    sequence = ctx.share.collaboration_sequence
    changed = True
    message = None
    if isinstance(command, s.ThreadCreate):
        thread = m.DiscussionThread(
            id=command.thread_id,
            scope_id=scope.id,
            author_id=ctx.user.id,
            created_sequence=sequence,
            activity_sequence=sequence,
        )
        db.add(thread)
        db.flush()
        db.add(
            m.DiscussionAnchorRevision(
                thread_id=thread.id,
                revision=1,
                actor_id=ctx.user.id,
                payload={**command.anchor.model_dump(), "entry_id": ctx.entry.id},
            )
        )
    if isinstance(command, (s.ThreadCreate, s.ReplyCreate)):
        if isinstance(command, s.ReplyCreate) and command.reopen:
            state_precondition(thread, command.expected_state_revision)
            if thread.state != "open":
                thread.state = "open"
                thread.state_revision += 1
        if db.get(m.DiscussionMessage, command.message_id) is not None:
            error(409, "identifier_unavailable")
        message = m.DiscussionMessage(
            id=command.message_id,
            thread_id=thread.id,
            author_id=ctx.user.id,
            created_sequence=sequence,
        )
        db.add(message)
        db.flush()
        db.add(
            m.DiscussionMessageRevision(
                message_id=message.id,
                revision=1,
                body=command.body.model_dump(mode="json"),
                actor_id=ctx.user.id,
            )
        )
    elif isinstance(command, (s.ThreadState, s.ThreadRemove)):
        state_precondition(thread, command.expected_state_revision)
        if isinstance(command, s.ThreadRemove):
            if not ctx.moderate or not command.reason.strip():
                error(403, "moderation_required")
            thread.deleted = True
            thread.removal_reason = command.reason
        else:
            changed = thread.state != command.state
            thread.state = command.state
        if changed:
            thread.state_revision += 1
    else:
        message = db.scalar(
            select(m.DiscussionMessage).where(
                m.DiscussionMessage.id == command.message_id,
                m.DiscussionMessage.thread_id == thread.id,
            )
        )
        if not message or message.deleted:
            error(404, "target_unavailable")
        if isinstance(command, (s.MessageEdit, s.MessageDelete)):
            if message.author_id != ctx.user.id and not (
                isinstance(command, s.MessageDelete)
                and ctx.moderate
                and command.reason
                and command.reason.strip()
            ):
                error(403, "author_required")
            if message.revision != command.expected_revision:
                error(409, "stale_message_revision")
            message.revision += 1
            message.edited_at = security.utcnow()
            message.deleted = isinstance(command, s.MessageDelete)
            body = (
                command.body.model_dump(mode="json") if isinstance(command, s.MessageEdit) else None
            )
            db.add(
                m.DiscussionMessageRevision(
                    message_id=message.id,
                    revision=message.revision,
                    body=body,
                    actor_id=ctx.user.id,
                    reason=getattr(command, "reason", None),
                )
            )
        elif isinstance(command, s.ReactionSet):
            reaction = db.get(m.DiscussionReaction, (message.id, ctx.user.id, command.emoji))
            changed = (reaction is not None) != command.present
            if command.present and reaction is None:
                db.add(
                    m.DiscussionReaction(
                        message_id=message.id, user_id=ctx.user.id, emoji=command.emoji
                    )
                )
            elif not command.present and reaction is not None:
                db.delete(reaction)
    thread.activity_sequence = sequence
    db.flush()
    result = {
        "operation_id": str(envelope.operation_id),
        "scope_id": str(scope.id),
        "thread_id": str(thread.id),
        "sequence": sequence,
        "state": thread.state,
        "state_revision": thread.state_revision,
        "removed": thread.deleted,
        "changed": changed,
    }
    if message is not None:
        result.update(
            message_id=str(message.id),
            message_revision=message.revision,
            message_deleted=message.deleted,
        )
    db.add(
        m.DiscussionEvent(
            share_id=ctx.share.id,
            sequence=sequence,
            scope_id=scope.id,
            kind=command.type,
            payload={**result, "actor_id": str(ctx.user.id)},
        )
    )
    db.add(
        m.DiscussionReceipt(
            actor_id=ctx.user.id,
            operation_id=envelope.operation_id,
            scope_id=scope.id,
            request_hash=request_hash,
            result=result,
        )
    )
    db.flush()
    db.commit()
    return result


def thread_data(thread):
    return {
        "id": str(thread.id),
        "author_id": str(thread.author_id) if thread.author_id else None,
        "state": thread.state,
        "state_revision": thread.state_revision,
        "anchor_revision": thread.anchor_revision,
        "removed": thread.deleted,
        "created_sequence": thread.created_sequence,
        "activity_sequence": thread.activity_sequence,
        "created_at": thread.created_at,
    }


def bounded_bodies(rows, body_of):
    """Leave room for metadata while bounding long message/history responses."""
    page, size = [], 0
    for row in rows:
        body_size = len(json.dumps(body_of(row), ensure_ascii=False).encode("utf-8"))
        if size + body_size > 384 * 1024:
            if not page:
                error(500, "invalid_stored_body")
            break
        page.append(row)
        size += body_size
    return page


def list_threads(db, token, share_id, entry_id, *, after=0, limit=50):
    ctx = context(db, token, share_id, entry_id)
    scope = scope_for(db, ctx)
    rows = (
        []
        if scope is None
        else list(
            db.scalars(
                select(m.DiscussionThread)
                .where(
                    m.DiscussionThread.scope_id == scope.id,
                    m.DiscussionThread.deleted.is_(False),
                    m.DiscussionThread.created_sequence > after,
                )
                .order_by(m.DiscussionThread.created_sequence)
                .limit(limit + 1)
            )
        )
    )
    page = rows[:limit]
    # This is live keyset browsing, not a paginated point-in-time snapshot.
    return {
        "threads": [thread_data(t) for t in page],
        "has_more": len(rows) > limit,
        "next_after": page[-1].created_sequence if page else after,
    }


def read_thread(db, token, share_id, entry_id, thread_id, *, after=0, limit=50):
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, scope_for(db, ctx), thread_id, include_removed=ctx.moderate)
    anchor = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    rows = db.execute(
        select(m.DiscussionMessage, m.DiscussionMessageRevision)
        .join(
            m.DiscussionMessageRevision,
            and_(
                m.DiscussionMessageRevision.message_id == m.DiscussionMessage.id,
                m.DiscussionMessageRevision.revision == m.DiscussionMessage.revision,
            ),
        )
        .where(
            m.DiscussionMessage.thread_id == thread.id, m.DiscussionMessage.created_sequence > after
        )
        .order_by(m.DiscussionMessage.created_sequence)
        .limit(limit + 1)
    ).all()
    page = bounded_bodies(rows[:limit], lambda row: row[1].body)
    reactions = {}
    if page:
        for message_id, emoji, total, mine in db.execute(
            select(
                m.DiscussionReaction.message_id,
                m.DiscussionReaction.emoji,
                func.count(),
                func.sum(case((m.DiscussionReaction.user_id == ctx.user.id, 1), else_=0)),
            )
            .where(m.DiscussionReaction.message_id.in_([message.id for message, _ in page]))
            .group_by(m.DiscussionReaction.message_id, m.DiscussionReaction.emoji)
            .order_by(m.DiscussionReaction.emoji)
        ):
            reactions.setdefault(message_id, []).append(
                {"emoji": emoji, "count": total, "mine": bool(mine)}
            )
    return {
        "thread": thread_data(thread),
        "anchor": anchor.payload,
        "messages": [
            {
                "id": str(message.id),
                "author_id": str(message.author_id) if message.author_id else None,
                "revision": message.revision,
                "deleted": message.deleted,
                "body": revision.body,
                "created_sequence": message.created_sequence,
                "created_at": message.created_at,
                "edited_at": message.edited_at,
                "reactions": [] if message.deleted else reactions.get(message.id, []),
            }
            for message, revision in page
        ],
        "has_more": len(rows) > len(page),
        "next_after": page[-1][0].created_sequence if page else after,
    }


def history(db, token, share_id, entry_id, thread_id, message_id, *, after=0, limit=50):
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, scope_for(db, ctx), thread_id, include_removed=ctx.moderate)
    message = db.scalar(
        select(m.DiscussionMessage).where(
            m.DiscussionMessage.id == message_id, m.DiscussionMessage.thread_id == thread.id
        )
    )
    if not message or (message.deleted and message.author_id != ctx.user.id and not ctx.moderate):
        error(404, "target_unavailable")
    rows = list(
        db.scalars(
            select(m.DiscussionMessageRevision)
            .where(
                m.DiscussionMessageRevision.message_id == message.id,
                m.DiscussionMessageRevision.revision > after,
            )
            .order_by(m.DiscussionMessageRevision.revision)
            .limit(limit + 1)
        )
    )
    page = bounded_bodies(rows[:limit], lambda row: row.body)
    return {
        "revisions": [
            {
                "revision": r.revision,
                "body": r.body,
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "created_at": r.created_at,
                "reason": r.reason,
            }
            for r in page
        ],
        "has_more": len(rows) > len(page),
        "next_after": page[-1].revision if page else after,
    }


def cursor_codec():
    key = hashlib.sha256(
        ("team-relay/discussion-cursor/v1:" + get_settings().jwt_secret).encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def cursor_value(ctx, sequence):
    return {
        "v": 1,
        "user": str(ctx.user.id),
        "share": str(ctx.share.id),
        "entry": ctx.entry.id,
        "epoch": str(ctx.share.collaboration_epoch),
        "sequence": sequence,
    }


def changes(db, token, share_id, entry_id, *, cursor=None, limit=100):
    ctx = context(db, token, share_id, entry_id)
    offset = 0
    if cursor:
        if len(cursor) > 2048:
            error(409, "snapshot_required")
        try:
            value = json.loads(cursor_codec().decrypt(cursor.encode(), ttl=30 * 86400))
            offset = value["sequence"]
            if (
                type(offset) is not int
                or offset < 0
                or value != cursor_value(ctx, offset)
                or offset > ctx.share.collaboration_sequence
            ):
                error(409, "snapshot_required")
        except (InvalidToken, ValueError, KeyError, TypeError):
            error(409, "snapshot_required")
    scope = scope_for(db, ctx)
    rows = (
        []
        if scope is None
        else list(
            db.scalars(
                select(m.DiscussionEvent)
                .where(
                    m.DiscussionEvent.share_id == ctx.share.id,
                    m.DiscussionEvent.scope_id == scope.id,
                    m.DiscussionEvent.sequence > offset,
                )
                .order_by(m.DiscussionEvent.sequence)
                .limit(limit + 1)
            )
        )
    )
    page = rows[:limit]
    has_more = len(rows) > limit
    through = page[-1].sequence if has_more else ctx.share.collaboration_sequence
    checkpoint = (
        cursor_codec()
        .encrypt(json.dumps(cursor_value(ctx, through), sort_keys=True).encode())
        .decode()
    )
    return {
        "events": [
            {"kind": event.kind, "sequence": event.sequence, "data": event.payload}
            for event in page
        ],
        "cursor": checkpoint,
        "has_more": has_more,
    }

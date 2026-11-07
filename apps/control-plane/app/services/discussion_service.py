"""Transactional whole-note discussion preview; no network calls while locks are held.

Lock order: actor user (SHARE), actor session (SHARE), share (UPDATE for
commands/SHARE for reads), membership (SHARE), entry (UPDATE for commands), then
thread/message. Existing managed rule/inventory writers lock the share.
User/session/membership changes conflict with the corresponding row lock.
A receipt never bypasses current auth.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api import deps
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as s
from app.services import (
    anchor_service,
    base_discussions,
    collaboration_auth,
    collaboration_policy,
    managed_service,
)


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
    db: Session,
    token: str,
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    *,
    write=False,
    entry_types=None,
    require_comment=True,
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
    entry_query = (
        select(m.ManagedEntry)
        .where(
            m.ManagedEntry.id == str(entry_id),
            m.ManagedEntry.share_id == share.id,
            m.ManagedEntry.deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    entry = db.scalar(entry_query.with_for_update() if write else entry_query)
    allowed_types = {"markdown", "image", "file"} if entry_types is None else entry_types
    if not entry or entry.record.get("type") not in allowed_types:
        error(404, "target_unavailable")
    if entry_types is None and entry.record.get("type") == "file":
        base_discussions.definition_for(db, Context(user, share, entry, moderate))
    permission = managed_service.permission(
        share, user, entry.path, managed_service.rules_for(db, share, user)
    )
    if permission not in {"read", "write"}:
        error(404, "target_unavailable")
    if (
        write
        and require_comment
        and not collaboration_policy.can_comment(
            share, user, entry.path, permission, collaboration_policy.rules_for(db, share, user)
        )
    ):
        error(403, "comment_forbidden")
    return Context(user, share, entry, moderate)


def scope_kind(ctx):
    return {"image": "image", "file": "base"}.get(ctx.entry.record.get("type"), "note")


def scope_for(db: Session, ctx: Context) -> m.DiscussionScope | None:
    scope = db.scalar(
        select(m.DiscussionScope).where(
            m.DiscussionScope.share_id == ctx.share.id, m.DiscussionScope.entry_id == ctx.entry.id
        )
    )
    if scope and scope.kind != scope_kind(ctx):
        # An inventory type change must not reinterpret a note's discussions as
        # image-file discussions (or vice versa). Explicit migration is separate.
        error(404, "target_unavailable")
    return scope


def participants(db, token, share_id, entry_id, *, after=None, limit=50):
    """Names only for active members who can read this discussion's actual note."""
    ctx = context(db, token, share_id, entry_id)
    query = select(m.User).where(
        m.User.is_active.is_(True),
        or_(
            m.User.id == ctx.share.owner_user_id,
            m.User.id.in_(
                select(m.ShareMember.user_id).where(m.ShareMember.share_id == ctx.share.id)
            ),
        ),
    )
    if after is not None:
        query = query.where(m.User.id > after)
    people = []
    for user in db.scalars(query.order_by(m.User.id)).yield_per(100):
        if managed_service.permission(
            ctx.share, user, ctx.entry.path, managed_service.rules_for(db, ctx.share, user)
        ) not in {"read", "write"}:
            continue
        people.append({"id": str(user.id), "name": user.email.split("@")[0]})
        if len(people) > limit:
            break
    page = people[:limit]
    return {
        "participants": page,
        "has_more": len(people) > limit,
        "next_after": page[-1]["id"] if page else (str(after) if after else None),
    }


def thread_for(
    db: Session,
    ctx: Context,
    thread_id: uuid.UUID,
    *,
    include_removed=False,
    write=False,
    scope=None,
) -> m.DiscussionThread:
    scope = scope or scope_for(db, ctx)
    if scope is None:
        error(404, "target_unavailable")
    thread = db.scalar(
        select(m.DiscussionThread)
        .where(m.DiscussionThread.id == thread_id, m.DiscussionThread.scope_id == scope.id)
        .execution_options(populate_existing=True)
    )
    if thread is None or (thread.deleted and not include_removed):
        error(404, "target_unavailable")
    base_discussions.authorize_thread(db, ctx, thread, write=write)
    return thread


def validate_mentions(db: Session, ctx: Context, body: s.MessageBody, anchor=None):
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
        if isinstance(anchor, s.BaseAnchor):
            try:
                base_discussions.validate(db, ctx, anchor, user=user)
            except HTTPException:
                error(422, "mention_unavailable")
    # Delivery will reauthorize recipients. Persist IDs, never a trusted client
    # display name, and never grant access as a side effect of a mention.


def state_precondition(thread, expected):
    if thread.state_revision != expected:
        error(409, "stale_thread_revision")


def document_context(db, token, share_id, entry_id):
    """Read permission is sufficient to discover server-owned lineage."""
    require_clean_session(db)
    try:
        ctx = context(db, token, share_id, entry_id)
        scope_for(db, ctx)
        if ctx.entry.record.get("type") == "image":
            if not get_settings().collaboration_image_files_enabled:
                error(404, "image_files_disabled")
        elif ctx.entry.record.get("type") == "file":
            if not base_discussions.enabled():
                error(404, "base_discussions_disabled")
        elif not anchor_service.enabled():
            error(404, "text_anchors_disabled")
        entry = db.scalar(
            select(m.ManagedEntry)
            .where(m.ManagedEntry.id == ctx.entry.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry.collaboration_generation is None:
            entry.collaboration_generation = uuid.uuid4()
        result = {
            "protocol_version": 1,
            "entry_id": entry.id,
            "generation": str(entry.collaboration_generation),
        }
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def existing_receipt(db, ctx, envelope, request_hash, *, allow_withdrawn=False):
    receipt = db.get(m.DiscussionReceipt, (ctx.user.id, envelope.operation_id))
    if receipt is None:
        return None
    scope = scope_for(db, ctx)
    if receipt.request_hash != request_hash or not scope or receipt.scope_id != scope.id:
        error(409, "operation_id_conflict")
    result = dict(receipt.result)
    if result.get("withdrawn") is True and not allow_withdrawn:
        error(410, "operation_withdrawn")
    if ctx.entry.record.get("type") == "file" and result.get("withdrawn") is not True:
        thread_for(
            db, ctx, uuid.UUID(result["thread_id"]), include_removed=True, write=True, scope=scope
        )
    db.commit()
    return result


def withdraw_command(db, token, envelope):
    """Resolve an ambiguous send before replacing it, under the command lock.

    A normal receipt wins. Otherwise the SAME receipt primary key becomes a
    permanent withdrawal: a concurrent/late send cannot accept that operation.
    No target proof or object-store I/O is needed to retire the identity.
    """
    require_clean_session(db)
    envelope = envelope.model_copy(deep=True)
    try:
        request_hash = anchor_service.command_hash(envelope)
        ctx = context(db, token, envelope.share_id, envelope.entry_id, write=True)
        prior = existing_receipt(db, ctx, envelope, request_hash, allow_withdrawn=True)
        if prior is not None:
            if prior.get("withdrawn") is True:
                return dict(
                    status="withdrawn",
                    operation_id=str(envelope.operation_id),
                    entry_id=str(envelope.entry_id),
                )
            return dict(status="accepted", result=prior)
        scope = scope_for(db, ctx)
        if scope is None:
            scope = m.DiscussionScope(
                share_id=ctx.share.id,
                entry_id=ctx.entry.id,
                kind=scope_kind(ctx),
            )
            db.add(scope)
            db.flush()
        db.add(
            m.DiscussionReceipt(
                actor_id=ctx.user.id,
                operation_id=envelope.operation_id,
                scope_id=scope.id,
                request_hash=request_hash,
                result={"withdrawn": True},
            )
        )
        db.commit()
        return dict(
            status="withdrawn",
            operation_id=str(envelope.operation_id),
            entry_id=str(envelope.entry_id),
        )
    except IntegrityError:
        db.rollback()
        error(409, "operation_id_conflict")
    except OperationalError:
        db.rollback()
        error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        error(500, "discussion_storage_error")
    except Exception:
        db.rollback()
        raise


def validate_image_asset(db, ctx, anchor):
    if not isinstance(anchor, s.ImageAnchor):
        return
    if not get_settings().collaboration_image_anchors_enabled:
        error(409, "image_anchors_disabled")
    if not isinstance(anchor.asset, s.ManagedImage):
        return
    # The share lock held by context also serializes inventory/rule writers.
    # This reference never authorizes a download or trusts a client-provided path.
    asset = db.scalar(
        select(m.ManagedEntry)
        .where(
            m.ManagedEntry.id == str(anchor.asset.entry_id),
            m.ManagedEntry.share_id == ctx.share.id,
            m.ManagedEntry.deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if (
        not asset
        or asset.record.get("type") != "image"
        or managed_service.permission(
            ctx.share, ctx.user, asset.path, managed_service.rules_for(db, ctx.share, ctx.user)
        )
        not in {"read", "write"}
    ):
        error(404, "image_unavailable")
    # Offline comments may describe an older image. Preserve the observed hash
    # as evidence instead of relabeling old coordinates with the current hash.


def validate_anchor_target(ctx, anchor):
    if isinstance(anchor, s.BaseAnchor) != (ctx.entry.record.get("type") == "file"):
        error(422, "invalid_anchor_scope")
    image_file = isinstance(anchor, s.ImageFileAnchor)
    if image_file != (ctx.entry.record.get("type") == "image"):
        error(422, "invalid_anchor_scope")
    if image_file:
        if not get_settings().collaboration_image_files_enabled:
            error(409, "image_files_disabled")
        if str(anchor.asset.entry_id) != ctx.entry.id:
            error(422, "invalid_anchor_scope")
        if anchor.generation != ctx.entry.collaboration_generation:
            error(409, "anchor_generation_mismatch")
        # context() already locked and authorized this exact live image entry.
        # Preserve the observed version, including older offline image hashes.


def prepare_text_anchor(db, token, envelope):
    ctx = context(db, token, envelope.share_id, envelope.entry_id, write=True)
    prior = existing_receipt(db, ctx, envelope, anchor_service.command_hash(envelope))
    if prior is not None:
        return prior
    validate_anchor_target(ctx, envelope.command.anchor)
    if not anchor_service.enabled():
        error(409, "text_anchors_disabled")
    anchor = s.source_anchor(envelope.command.anchor)
    if anchor.document_id != envelope.entry_id:
        error(422, "invalid_anchor")
    if anchor.generation != ctx.entry.collaboration_generation:
        error(409, "anchor_generation_mismatch")
    validate_mentions(db, ctx, envelope.command.body)
    validate_image_asset(db, ctx, envelope.command.anchor)
    # Release every authorization/identity lock before any private network I/O.
    # The final command transaction reacquires and rechecks all of them.
    db.commit()
    return anchor_service.validate(envelope)


def apply_command(db: Session, token: str, envelope: s.CommandEnvelope) -> dict:
    # Do not roll back or expire a caller's unrelated pending changes.
    require_clean_session(db)
    envelope = envelope.model_copy(deep=True)
    try:
        text = (
            isinstance(envelope.command, s.ThreadCreate)
            and s.source_anchor(envelope.command.anchor) is not None
        )
        group = (
            isinstance(envelope.command, s.ThreadCreate)
            and isinstance(envelope.command.anchor, s.BaseAnchor)
            and (
                isinstance(envelope.command.anchor.target, s.BaseGroupTarget)
                or isinstance(envelope.command.anchor.target, s.BaseTimelineRangeTarget)
                and envelope.command.anchor.target.lane is not None
            )
        )
        limit = 256 * 1024 if text or group else 65536
        if len(envelope.canonical_json().encode("utf-8")) > limit:
            error(413, "command_too_large")
        proof = prepare_text_anchor(db, token, envelope) if text else None
        if isinstance(proof, dict):
            return proof
        return _apply_command(db, token, envelope, proof)
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


def _apply_command(db: Session, token: str, envelope: s.CommandEnvelope, proof=None) -> dict:
    request_hash = hashlib.sha256(envelope.canonical_json().encode("utf-8")).hexdigest()
    ctx = context(db, token, envelope.share_id, envelope.entry_id, write=True)
    scope = scope_for(db, ctx)
    prior = existing_receipt(db, ctx, envelope, request_hash)
    if prior is not None:
        return prior
    command = envelope.command
    if isinstance(command, s.ThreadCreate):
        validate_anchor_target(ctx, command.anchor)
        if isinstance(command.anchor, s.BaseAnchor):
            base_discussions.validate(db, ctx, command.anchor, creation=True, write=True)
    if isinstance(command, s.ThreadCreate) and s.source_anchor(command.anchor) is not None:
        if not anchor_service.enabled():
            error(409, "text_anchors_disabled")
        if not isinstance(proof, anchor_service.AnchorProof):
            error(503, "anchor_validation_unavailable")
        proof.check(envelope, ctx.entry.collaboration_generation)
        validate_image_asset(db, ctx, command.anchor)
    target_anchor = command.anchor if isinstance(command, s.ThreadCreate) else None
    if isinstance(command, s.ThreadCreate):
        if scope is None:
            scope = m.DiscussionScope(
                share_id=ctx.share.id,
                entry_id=ctx.entry.id,
                kind=scope_kind(ctx),
            )
            db.add(scope)
            db.flush()
        if db.get(m.DiscussionThread, command.thread_id) is not None:
            error(409, "identifier_unavailable")
    else:
        thread = thread_for(db, ctx, command.thread_id, scope=scope, write=True)
        target_anchor = base_discussions.authorize_thread(db, ctx, thread, write=True)
    if hasattr(command, "body"):
        validate_mentions(db, ctx, command.body, target_anchor)
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
                payload={**command.anchor.model_dump(mode="json"), "entry_id": ctx.entry.id},
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
    if isinstance(command, (s.ThreadCreate, s.ReplyCreate, s.MessageEdit, s.MessageDelete)):
        from app.services import discussion_search_index

        discussion_search_index.update(
            db, message, command.body.model_dump(mode="json") if hasattr(command, "body") else None
        )
    if hasattr(command, "body") and command.body.picture_ids():
        from app.services.discussion_pictures import attach

        # References, immutable history, receipts and events share a commit.
        # Uploading a picture alone never publishes it to other members.
        db.flush()
        attach(db, ctx, scope, command.body, message)
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
            payload={
                **result,
                "actor_id": str(ctx.user.id),
                **(
                    {"view_id": str(target_anchor.view_id)}
                    if isinstance(target_anchor, s.BaseAnchor)
                    else {}
                ),
            },
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
    from app.services.discussion_inbox import enqueue

    enqueue(db, ctx, command, thread, message, sequence)
    db.flush()
    if proof is not None:
        proof.check(envelope, ctx.entry.collaboration_generation)
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


def bounded_bodies(rows, body_of, budget=384 * 1024):
    """Leave room for metadata while bounding long message/history responses."""
    page, size = [], 0
    for row in rows:
        body_size = len(json.dumps(body_of(row), ensure_ascii=False).encode("utf-8"))
        if size + body_size > budget:
            if not page:
                error(500, "invalid_stored_body")
            break
        page.append(row)
        size += body_size
    return page


def requested_view(db, ctx, view_id):
    if ctx.entry.record.get("type") == "file":
        if view_id is None:
            error(422, "base_view_required")
        base_discussions.view_for(db, ctx, view_id)
    elif view_id is not None:
        error(422, "invalid_anchor_scope")


def list_threads(db, token, share_id, entry_id, *, after=0, limit=50, view_id=None):
    ctx = context(db, token, share_id, entry_id)
    requested_view(db, ctx, view_id)
    scope = scope_for(db, ctx)
    rows = []
    if scope is not None:
        query = select(m.DiscussionThread).where(
            m.DiscussionThread.scope_id == scope.id,
            m.DiscussionThread.deleted.is_(False),
            m.DiscussionThread.created_sequence > after,
        )
        if view_id is not None:
            query = query.join(
                m.DiscussionAnchorRevision,
                and_(
                    m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                    m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
                ),
            ).where(m.DiscussionAnchorRevision.payload["view_id"].as_string() == str(view_id))
        rows = list(
            db.scalars(query.order_by(m.DiscussionThread.created_sequence).limit(limit + 1))
        )
    page = rows[:limit]
    through = page[-1].created_sequence if page else after
    # This is live keyset browsing, not a paginated point-in-time snapshot.
    # Bases pages bound scanned candidates, not just visible results. Advancing
    # over hidden records cannot leak their IDs or make an empty page loop.
    return {
        "threads": [thread_data(t) for t in page if base_discussions.can_read_thread(db, ctx, t)],
        "has_more": len(rows) > limit,
        "next_after": through,
        **({"view_id": str(view_id), "scanned_through": through} if view_id is not None else {}),
    }


def read_thread(db, token, share_id, entry_id, thread_id, *, after=0, limit=50):
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, ctx, thread_id, include_removed=ctx.moderate)
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
    page = bounded_bodies(
        rows[:limit],
        lambda row: row[1].body,
        384 * 1024 - len(json.dumps(anchor.payload, ensure_ascii=False).encode("utf-8")),
    )
    return {
        "thread": thread_data(thread),
        "anchor": anchor.payload,
        "messages": message_data(db, ctx, page),
        "has_more": len(rows) > len(page),
        "next_after": page[-1][0].created_sequence if page else after,
    }


def message_data(db, ctx, rows):
    reactions = {}
    if rows:
        for message_id, emoji, total, mine in db.execute(
            select(
                m.DiscussionReaction.message_id,
                m.DiscussionReaction.emoji,
                func.count(),
                func.sum(case((m.DiscussionReaction.user_id == ctx.user.id, 1), else_=0)),
            )
            .where(m.DiscussionReaction.message_id.in_([message.id for message, _ in rows]))
            .group_by(m.DiscussionReaction.message_id, m.DiscussionReaction.emoji)
            .order_by(m.DiscussionReaction.emoji)
        ):
            reactions.setdefault(message_id, []).append(
                {"emoji": emoji, "count": total, "mine": bool(mine)}
            )
    return [
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
        for message, revision in rows
    ]


def read_thread_state(db, token, share_id, entry_id, thread_id):
    """Removal metadata is readable without exposing removed text or anchor quotes."""
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, ctx, thread_id, include_removed=True)
    result = {"thread": thread_data(thread)}
    if not thread.deleted:
        anchor = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
        result["anchor"] = anchor.payload
    return result


def read_message(db, token, share_id, entry_id, thread_id, message_id):
    """Fetch the current authorized revision/reactions without replaying a long thread."""
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, ctx, thread_id, include_removed=ctx.moderate)
    row = db.execute(
        select(m.DiscussionMessage, m.DiscussionMessageRevision)
        .join(
            m.DiscussionMessageRevision,
            and_(
                m.DiscussionMessageRevision.message_id == m.DiscussionMessage.id,
                m.DiscussionMessageRevision.revision == m.DiscussionMessage.revision,
            ),
        )
        .where(m.DiscussionMessage.thread_id == thread.id, m.DiscussionMessage.id == message_id)
    ).first()
    if row is None:
        error(404, "target_unavailable")
    anchor = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    return {
        "thread": thread_data(thread),
        "anchor": anchor.payload,
        "message": message_data(db, ctx, [row])[0],
    }


def history(db, token, share_id, entry_id, thread_id, message_id, *, after=0, limit=50):
    ctx = context(db, token, share_id, entry_id)
    thread = thread_for(db, ctx, thread_id, include_removed=ctx.moderate)
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


def cursor_value(ctx, sequence, view_id=None):
    return {
        "v": 1,
        "user": str(ctx.user.id),
        "share": str(ctx.share.id),
        "entry": ctx.entry.id,
        "epoch": str(ctx.share.collaboration_epoch),
        "sequence": sequence,
        **({"view": str(view_id)} if view_id is not None else {}),
    }


def changes(
    db,
    token,
    share_id,
    entry_id,
    *,
    cursor=None,
    limit=100,
    view_id=None,
    include_timeline_markers=False,
):
    ctx = context(db, token, share_id, entry_id)
    requested_view(db, ctx, view_id)
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
                or value != cursor_value(ctx, offset, view_id)
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
                    *(
                        [m.DiscussionEvent.payload["view_id"].as_string() == str(view_id)]
                        if view_id is not None
                        else []
                    ),
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
        .encrypt(json.dumps(cursor_value(ctx, through, view_id), sort_keys=True).encode())
        .decode()
    )
    marker_version = {}
    if include_timeline_markers and view_id is not None:
        # Revision hint only: the ordinary authorized catalog read still proves
        # every placement. Piggyback on active discussion polling; old clients
        # neither request nor receive this additive metadata.
        definition = base_discussions.definition_for(db, ctx)
        catalog = db.get(m.BaseTimelineMarkerCatalog, definition.id)
        marker_version = {
            "timeline_markers": {
                "protocol_version": 1,
                "entry_id": ctx.entry.id,
                "view_id": str(view_id),
                "definition_id": str(definition.id),
                "generation": str(definition.generation),
                "registry_revision": definition.revision,
                "catalog_revision": catalog.revision if catalog else 0,
            }
        }
    return {
        "events": [
            {"kind": event.kind, "sequence": event.sequence, "data": event.payload}
            for event in page
            if view_id is None or event_readable(db, ctx, event)
        ],
        "cursor": checkpoint,
        "has_more": has_more,
        **({"view_id": str(view_id), "filtered": True} if view_id is not None else {}),
        **marker_version,
    }


def event_readable(db, ctx, event):
    thread = db.get(m.DiscussionThread, uuid.UUID(event.payload["thread_id"]))
    return thread is not None and base_discussions.can_read_thread(db, ctx, thread)

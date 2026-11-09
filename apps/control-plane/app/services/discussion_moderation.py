"""Explicit, bounded and audited reads of removed discussion content.

Ordinary routes never call this service. No restoration, command replay, outbox,
cache population or automatic polling. Commit an audit record before disclosure.
"""

import copy
import threading
import uuid
from datetime import timedelta, timezone

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.responses import JSONResponse, Response

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import base_discussions
from app.services import discussion_pictures as pictures
from app.services import discussion_service as discussions

slots = threading.BoundedSemaphore(2)


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_moderation_recovery_enabled
    )


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def view_for(db, ctx, thread):
    row = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    if row and row.payload.get("type") != "base" and ctx.entry.record.get("type") != "file":
        return None
    return base_discussions.stored_anchor(row.payload if row else None).view_id


def deadline(thread, message):
    if not (thread.deleted or message.deleted):
        return None
    if thread.content_purged_at is not None or message.content_purged_at is not None:
        return None
    removed = [row.deleted_at for row in (thread, message) if row.deleted]
    if any(stamp is None for stamp in removed):
        return None
    until = min(aware(stamp) for stamp in removed) + timedelta(
        days=get_settings().collaboration_removed_content_retention_days
    )
    return until if security.utcnow() < until else None


def identity(db, ctx, thread, message):
    view = view_for(db, ctx, thread)
    return {
        "entry_id": ctx.entry.id,
        "view_id": str(view) if view else None,
        "thread_id": str(thread.id),
        "message_id": str(message.id),
    }


def target(db, token, share_id, entry_id, thread_id, message_id, *, view_id=None):
    ctx = discussions.context(db, token, share_id, entry_id)
    if not ctx.moderate:
        discussions.error(403, "moderation_required")
    thread = discussions.thread_for(db, ctx, thread_id, include_removed=True)
    if view_id is not None and view_for(db, ctx, thread) != view_id:
        discussions.error(404, "target_unavailable")
    message = db.scalar(
        select(m.DiscussionMessage).where(
            m.DiscussionMessage.id == message_id, m.DiscussionMessage.thread_id == thread.id
        )
    )
    until = deadline(thread, message) if message else None
    if until is None:
        discussions.error(404, "target_unavailable")
    return ctx, thread, message, until


def audit(db, ctx, thread, message, request, *, resource, revisions, picture=None, variant=None):
    log = m.AuditLog(
        id=uuid.uuid4(),
        action=m.AuditAction.DISCUSSION_RECOVERY_READ,
        actor_user_id=ctx.user.id,
        target_share_id=ctx.share.id,
        details={
            "protocol_version": 1,
            "resource": resource,
            "reason": request.reason,
            "share_id": str(ctx.share.id),
            "entry_id": ctx.entry.id,
            "thread_id": str(thread.id),
            "message_id": str(message.id),
            "revisions": revisions,
            **({"picture_id": str(picture), "variant": variant} if picture else {}),
        },
    )
    db.add(log)
    return log.id


def _read(db, work):
    if not enabled():
        discussions.error(404, "moderation_recovery_disabled")
    discussions.require_clean_session(db)
    if not slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "moderation_recovery_busy"}, headers={"Retry-After": "2"})
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
        response = work()
        # Response bytes are fixed under the authorization locks. A failed audit
        # commit must never return the prepared content or a successful response.
        db.commit()
        return response
    except OperationalError:
        db.rollback()
        discussions.error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        discussions.error(500, "moderation_storage_error")
    except Exception:
        db.rollback()
        raise
    finally:
        slots.release()


def history(db, token, share_id, entry_id, thread_id, message_id, request):
    def capture():
        ctx, thread, message, until = target(
            db, token, share_id, entry_id, thread_id, message_id, view_id=request.view_id
        )
        rows = list(
            db.scalars(
                select(m.DiscussionMessageRevision)
                .where(
                    m.DiscussionMessageRevision.message_id == message.id,
                    m.DiscussionMessageRevision.revision > request.after,
                )
                .order_by(m.DiscussionMessageRevision.revision)
                .limit(request.limit + 1)
            )
        )
        page = discussions.bounded_bodies(rows[: request.limit], lambda row: row.body)
        audit_id = audit(
            db,
            ctx,
            thread,
            message,
            request,
            resource="message_history",
            revisions=[row.revision for row in page],
        )
        response = JSONResponse(
            jsonable_encoder(
                {
                    "protocol_version": 1,
                    "audit_id": str(audit_id),
                    **identity(db, ctx, thread, message),
                    "recoverable_until": until,
                    "thread_removed": thread.deleted,
                    "message_removed": message.deleted,
                    "thread_removal_reason": thread.removal_reason if thread.deleted else None,
                    "revisions": [
                        {
                            "revision": row.revision,
                            "body": row.body,
                            "actor_id": str(row.actor_id) if row.actor_id else None,
                            "created_at": discussions.utc_timestamp(row.created_at),
                            "reason": row.reason,
                        }
                        for row in page
                    ],
                    "has_more": len(rows) > len(page),
                    "next_after": page[-1].revision if page else request.after,
                }
            ),
            headers={"Cache-Control": "no-store"},
        )
        if len(response.body) > 512 * 1024:
            discussions.error(413, "moderation_page_too_large")
        return response

    return _read(db, capture)


def _picture(db, token, share_id, entry_id, thread_id, message_id, picture_id, request):
    ctx, thread, message, until = target(
        db, token, share_id, entry_id, thread_id, message_id, view_id=request.view_id
    )
    picture = db.scalar(
        select(m.DiscussionPicture)
        .where(
            m.DiscussionPicture.id == picture_id,
            m.DiscussionPicture.scope_id == thread.scope_id,
            m.DiscussionPicture.state == "attached",
        )
        .execution_options(populate_existing=True)
    )
    if picture is None:
        discussions.error(404, "target_unavailable")
    revision = db.scalar(
        select(m.DiscussionPictureReference.revision)
        .where(
            m.DiscussionPictureReference.picture_id == picture.id,
            m.DiscussionPictureReference.message_id == message.id,
        )
        .order_by(m.DiscussionPictureReference.revision)
        .limit(1)
    )
    if picture.state != "attached" or revision is None:
        discussions.error(404, "target_unavailable")
    return ctx, thread, message, picture, revision


def picture_metadata(db, token, share_id, entry_id, thread_id, message_id, picture_id, request):
    def capture():
        ctx, thread, message, picture, revision = _picture(
            db, token, share_id, entry_id, thread_id, message_id, picture_id, request
        )
        audit_id = audit(
            db,
            ctx,
            thread,
            message,
            request,
            resource="picture_metadata",
            revisions=[revision],
            picture=picture.id,
        )
        return JSONResponse(
            {
                "protocol_version": 1,
                "audit_id": str(audit_id),
                **identity(db, ctx, thread, message),
                "picture": pictures.public_data(picture),
            },
            headers={"Cache-Control": "no-store"},
        )

    return _read(db, capture)


def picture_content(
    db, token, share_id, entry_id, thread_id, message_id, picture_id, variant, request
):
    if variant not in {"content", "thumbnail"}:
        discussions.error(404, "target_unavailable")

    def prepare():
        _, _, _, picture, _ = _picture(
            db, token, share_id, entry_id, thread_id, message_id, picture_id, request
        )
        rendition = (picture.renditions or {}).get(variant)
        if rendition is None:
            discussions.error(404, "target_unavailable")
        return copy.deepcopy(rendition)

    rendition = _read(db, prepare)
    # Release SQL locks before storage I/O. Use the ordinary download capacity
    # bound, but never its ordinary read route or the client picture cache.
    if not pictures.download_slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "picture_capacity"}, headers={"Retry-After": "2"})
    try:
        data = pictures.get_rendition(rendition)

        def deliver():
            ctx, thread, message, picture, revision = _picture(
                db, token, share_id, entry_id, thread_id, message_id, picture_id, request
            )
            if (picture.renditions or {}).get(variant) != rendition:
                discussions.error(409, "picture_changed")
            audit_id = audit(
                db,
                ctx,
                thread,
                message,
                request,
                resource="picture_content",
                revisions=[revision],
                picture=picture.id,
                variant=variant,
            )
            return Response(
                data,
                media_type=rendition["mime"],
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Discussion-Audit-Id": str(audit_id),
                },
            )

        return _read(db, deliver)
    finally:
        pictures.download_slots.release()


def catalog(db, token, share_id, entry_id, request):
    """A bounded, explicitly audited inventory; never returns bodies or anchors."""

    def capture():
        ctx = discussions.context(db, token, share_id, entry_id)
        if not ctx.moderate:
            discussions.error(403, "moderation_required")
        discussions.requested_view(db, ctx, request.view_id)
        scope = discussions.scope_for(db, ctx)
        rows = []
        if scope is not None:
            query = (
                select(m.DiscussionMessage, m.DiscussionThread)
                .join(m.DiscussionThread, m.DiscussionThread.id == m.DiscussionMessage.thread_id)
                .where(
                    m.DiscussionThread.scope_id == scope.id,
                    m.DiscussionThread.content_purged_at.is_(None),
                    m.DiscussionMessage.content_purged_at.is_(None),
                    or_(
                        m.DiscussionThread.deleted.is_(True), m.DiscussionMessage.deleted.is_(True)
                    ),
                    m.DiscussionMessage.created_sequence > request.after,
                )
            )
            if request.view_id is not None:
                query = query.join(
                    m.DiscussionAnchorRevision,
                    and_(
                        m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                        m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
                    ),
                ).where(
                    m.DiscussionAnchorRevision.payload["view_id"].as_string()
                    == str(request.view_id)
                )
            rows = db.execute(
                query.order_by(m.DiscussionMessage.created_sequence).limit(request.limit + 1)
            ).all()
        items = []
        for message, thread in rows[: request.limit]:
            until = deadline(thread, message)
            if until is None or not base_discussions.can_read_thread(db, ctx, thread):
                continue
            items.append(
                {
                    "thread_id": str(thread.id),
                    "message_id": str(message.id),
                    "author_id": str(message.author_id) if message.author_id else None,
                    "created_at": discussions.utc_timestamp(message.created_at),
                    "removed_at": min(
                        aware(row.deleted_at) for row in (thread, message) if row.deleted
                    ),
                    "recoverable_until": until,
                    "thread_removed": thread.deleted,
                    "message_removed": message.deleted,
                    "revision": message.revision,
                }
            )
        audit_id = uuid.uuid4()
        db.add(
            m.AuditLog(
                id=audit_id,
                action=m.AuditAction.DISCUSSION_RECOVERY_READ,
                actor_user_id=ctx.user.id,
                target_share_id=ctx.share.id,
                details={
                    "protocol_version": 1,
                    "resource": "removed_catalog",
                    "reason": request.reason,
                    "share_id": str(ctx.share.id),
                    "entry_id": ctx.entry.id,
                    "view_id": str(request.view_id) if request.view_id else None,
                    "messages": [
                        {"thread_id": item["thread_id"], "message_id": item["message_id"]}
                        for item in items
                    ],
                },
            )
        )
        return JSONResponse(
            jsonable_encoder(
                {
                    "protocol_version": 1,
                    "audit_id": str(audit_id),
                    "entry_id": ctx.entry.id,
                    "view_id": str(request.view_id) if request.view_id else None,
                    "items": items,
                    "has_more": len(rows) > request.limit,
                    "next_after": rows[min(len(rows), request.limit) - 1][0].created_sequence
                    if rows
                    else request.after,
                }
            ),
            headers={"Cache-Control": "no-store"},
        )

    return _read(db, capture)

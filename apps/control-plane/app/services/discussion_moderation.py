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
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.responses import JSONResponse, Response

from app.core import security
from app.core.config import get_settings
from app.db import models as m
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


def target(db, token, share_id, entry_id, thread_id, message_id):
    ctx = discussions.context(db, token, share_id, entry_id)
    if not ctx.moderate:
        discussions.error(403, "moderation_required")
    thread = discussions.thread_for(db, ctx, thread_id, include_removed=True)
    message = db.scalar(
        select(m.DiscussionMessage).where(
            m.DiscussionMessage.id == message_id, m.DiscussionMessage.thread_id == thread.id
        )
    )
    if message is None or not (thread.deleted or message.deleted):
        discussions.error(404, "target_unavailable")
    if thread.content_purged_at is not None or message.content_purged_at is not None:
        discussions.error(404, "target_unavailable")
    removed = [row.deleted_at for row in (thread, message) if row.deleted]
    if any(stamp is None for stamp in removed):
        discussions.error(404, "target_unavailable")
    until = min(aware(stamp) for stamp in removed) + timedelta(
        days=get_settings().collaboration_removed_content_retention_days
    )
    if security.utcnow() >= until:
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
        ctx, thread, message, until = target(db, token, share_id, entry_id, thread_id, message_id)
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
                    "thread_id": str(thread.id),
                    "message_id": str(message.id),
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


def _picture(db, token, share_id, entry_id, thread_id, message_id, picture_id):
    ctx, thread, message, until = target(db, token, share_id, entry_id, thread_id, message_id)
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
            db, token, share_id, entry_id, thread_id, message_id, picture_id
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
            db, token, share_id, entry_id, thread_id, message_id, picture_id
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
                db, token, share_id, entry_id, thread_id, message_id, picture_id
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

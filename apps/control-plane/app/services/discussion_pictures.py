"""Private picture lifecycle; authorization locks never span storage or decoding.

The immutable source digest makes upload retries safe. Message revisions and
picture references commit together. Downloads apply the same visibility rules
as message history, including removal, and recheck after object-store I/O.
"""

import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from functools import wraps
from io import BytesIO

from fastapi import HTTPException
from minio.error import S3Error
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.clients import storage_client
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_picture_codec as codec
from app.services import discussion_service as discussions

# Admission occurs before receiving upload bodies. No unbounded decoder queue.
# Each server process has at most one upload/decode and two content reads.
upload_slots = threading.BoundedSemaphore(1)
download_slots = threading.BoundedSemaphore(2)
STAGING_TTL = timedelta(hours=24)
STAGING_QUOTA = 100 * 1024 * 1024
STAGING_COUNT = 100


def namespace(share_id, scope_id, picture_id):
    return f"discussion-pictures/{share_id}/{scope_id}/{picture_id}/"


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_pictures_enabled
    )


def require_enabled():
    if not enabled():
        discussions.error(409, "pictures_disabled")


def transactional(fn):
    @wraps(fn)
    def run(db, *args, **kwargs):
        discussions.require_clean_session(db)
        try:
            return fn(db, *args, **kwargs)
        except IntegrityError:
            db.rollback()
            discussions.error(409, "identifier_unavailable")
        except OperationalError:
            db.rollback()
            discussions.error(503, "retryable_transaction")
        except SQLAlchemyError:
            db.rollback()
            discussions.error(500, "picture_storage_error")
        except Exception:
            db.rollback()
            raise

    return run


def expired(picture):
    expires = picture.expires_at
    if expires.tzinfo is None:  # SQLite test adapter; Postgres returns aware timestamps.
        expires = expires.replace(tzinfo=timezone.utc)
    return picture.state == "expired" or (
        picture.state != "attached" and expires <= security.utcnow()
    )


def picture_for(db, ctx, picture_id, *, own=False):
    scope = discussions.scope_for(db, ctx)
    picture = db.get(m.DiscussionPicture, picture_id)
    if (
        picture is None
        or scope is None
        or picture.scope_id != scope.id
        or (own and picture.owner_id != ctx.user.id)
    ):
        discussions.error(404, "target_unavailable")
    if expired(picture):
        # Only the original author learns about an expired staging identifier.
        discussions.error(
            410 if picture.owner_id == ctx.user.id else 404,
            "picture_expired" if picture.owner_id == ctx.user.id else "target_unavailable",
        )
    return picture


def public_data(picture):
    expires_at = picture.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return {
        "picture_id": str(picture.id),
        "state": picture.state,
        "source_sha256": picture.source_sha256,
        "source_size": picture.source_size,
        "source_mime": picture.source_mime,
        "expires_at": expires_at if picture.state != "attached" else None,
        "renditions": {
            name: {key: value for key, value in rendition.items() if key != "key"}
            for name, rendition in (picture.renditions or {}).items()
        },
    }


@transactional
def stage(db, token, share_id, entry_id, payload):
    ctx = discussions.context(db, token, share_id, entry_id, write=True)
    require_enabled()
    scope = discussions.scope_for(db, ctx)
    prior = db.get(m.DiscussionPicture, payload.picture_id)
    if prior is not None:
        picture = picture_for(db, ctx, payload.picture_id, own=True)
        if picture.source_sha256 != payload.sha256 or picture.source_size != payload.size:
            discussions.error(409, "picture_identity_conflict")
        result = public_data(picture)
        db.commit()
        return result
    count, size = db.execute(
        select(func.count(), func.coalesce(func.sum(m.DiscussionPicture.source_size), 0))
        .select_from(m.DiscussionPicture)
        .join(m.DiscussionScope)
        .where(
            m.DiscussionScope.share_id == ctx.share.id,
            m.DiscussionPicture.owner_id == ctx.user.id,
            m.DiscussionPicture.state.in_(["staged", "verified"]),
            m.DiscussionPicture.expires_at > security.utcnow(),
        )
    ).one()
    if count >= STAGING_COUNT or size + payload.size > STAGING_QUOTA:
        discussions.error(409, "picture_staging_quota")
    if scope is None:
        scope = m.DiscussionScope(
            share_id=ctx.share.id,
            entry_id=ctx.entry.id,
            kind=discussions.scope_kind(ctx),
        )
        db.add(scope)
        db.flush()
    retired = db.get(
        m.DiscussionPictureRetirement,
        (
            get_settings().collaboration_pictures_bucket,
            namespace(ctx.share.id, scope.id, payload.picture_id),
        ),
    )
    if retired is not None and retired.retired_at is not None:
        # Cleanup commits this guard under the same Share lock before object I/O.
        discussions.error(409, "identifier_unavailable")
    now = security.utcnow()
    picture = m.DiscussionPicture(
        id=payload.picture_id,
        scope_id=scope.id,
        owner_id=ctx.user.id,
        source_sha256=payload.sha256,
        source_size=payload.size,
        state="staged",
        created_at=now,
        expires_at=now + STAGING_TTL,
    )
    db.add(picture)
    db.flush()
    result = public_data(picture)
    db.commit()
    return result


@dataclass(frozen=True)
class UploadTicket:
    picture_id: uuid.UUID
    scope_id: uuid.UUID
    sha256: str
    size: int


@transactional
def prepare_upload(db, token, share_id, entry_id, picture_id):
    ctx = discussions.context(db, token, share_id, entry_id, write=True)
    require_enabled()
    picture = picture_for(db, ctx, picture_id, own=True)
    ticket = UploadTicket(picture.id, picture.scope_id, picture.source_sha256, picture.source_size)
    db.commit()
    return ticket


def put_rendition(prefix, rendition):
    settings = get_settings()
    client = storage_client.get_minio_client()
    extension = "jpg" if rendition.mime == "image/jpeg" else "png"
    key = f"{prefix}/{rendition.sha256}.{extension}"
    try:
        with storage_client.storage_errors("discussion_picture_put"):
            # Bucket provisioning belongs to service setup, not comment grants.
            client.put_object(
                settings.collaboration_pictures_bucket,
                key,
                BytesIO(rendition.data),
                len(rendition.data),
                content_type=rendition.mime,
                num_parallel_uploads=1,
            )
    except S3Error:
        discussions.error(503, "picture_storage_unavailable")
    return dict(
        key=key,
        sha256=rendition.sha256,
        size=len(rendition.data),
        mime=rendition.mime,
        width=rendition.width,
        height=rendition.height,
    )


@transactional
def upload(db, token, share_id, entry_id, ticket, data):
    if len(data) != ticket.size or hashlib.sha256(data).hexdigest() != ticket.sha256:
        discussions.error(422, "picture_digest_mismatch")
    # Recheck after receiving the body, before expensive decoding/storage.
    ctx = discussions.context(db, token, share_id, entry_id, write=True)
    require_enabled()
    picture = picture_for(db, ctx, ticket.picture_id, own=True)
    if (
        picture.scope_id != ticket.scope_id
        or picture.source_sha256 != ticket.sha256
        or picture.source_size != ticket.size
    ):
        discussions.error(409, "picture_identity_conflict")
    if picture.state in {"verified", "attached"}:
        result = public_data(picture)
        db.commit()
        return result
    db.commit()  # No authorization locks or database transaction during I/O.
    verified = codec.verify(data)
    prefix = namespace(share_id, ticket.scope_id, ticket.picture_id) + "v1"
    renditions = {
        name: put_rendition(prefix, getattr(verified, name)) for name in ("content", "thumbnail")
    }
    ctx = discussions.context(db, token, share_id, entry_id, write=True)
    require_enabled()
    picture = picture_for(db, ctx, ticket.picture_id, own=True)
    # A concurrent successful retry wins. It is never replaced by this result.
    if picture.state == "staged":
        picture.renditions = renditions
        picture.source_mime = verified.source_mime
        picture.state = "verified"
        db.flush()
    result = public_data(picture)
    db.commit()
    return result


def attach(db, ctx, scope, body, message):
    ids = body.picture_ids()
    for picture_id in sorted(ids):
        picture = picture_for(db, ctx, picture_id, own=True)
        if picture.scope_id != scope.id or picture.state not in {"verified", "attached"}:
            discussions.error(409, "picture_not_verified")
        if not enabled():
            # A rollback switch stops new attachments but permits text edits
            # retaining pictures already present in this message's last revision.
            prior = db.get(
                m.DiscussionPictureReference, (picture.id, message.id, message.revision - 1)
            )
            if prior is None:
                discussions.error(409, "pictures_disabled")
        picture.state = "attached"
        db.add(
            m.DiscussionPictureReference(
                picture_id=picture.id, message_id=message.id, revision=message.revision
            )
        )


def readable(db, ctx, picture):
    if picture.state != "attached":
        if picture.owner_id != ctx.user.id:
            discussions.error(404, "target_unavailable")
        return
    # History is visible for live messages; deleted messages are visible only
    # to their author/moderator. Removed threads remain moderator-only.
    query = (
        select(m.DiscussionThread)
        .select_from(m.DiscussionPictureReference)
        .join(
            m.DiscussionMessage, m.DiscussionMessage.id == m.DiscussionPictureReference.message_id
        )
        .join(m.DiscussionThread, m.DiscussionThread.id == m.DiscussionMessage.thread_id)
        .where(
            m.DiscussionPictureReference.picture_id == picture.id,
            m.DiscussionThread.scope_id == picture.scope_id,
        )
    )
    if not ctx.moderate:
        query = query.where(
            m.DiscussionThread.deleted.is_(False),
            or_(
                m.DiscussionMessage.deleted.is_(False), m.DiscussionMessage.author_id == ctx.user.id
            ),
        )
    for thread in db.scalars(query.distinct()).yield_per(100):
        if discussions.base_discussions.can_read_thread(db, ctx, thread):
            return
    discussions.error(404, "target_unavailable")


@transactional
def metadata(db, token, share_id, entry_id, picture_id):
    ctx = discussions.context(db, token, share_id, entry_id)
    picture = picture_for(db, ctx, picture_id)
    readable(db, ctx, picture)
    result = public_data(picture)
    db.commit()
    return result


def get_rendition(rendition):
    settings = get_settings()
    client = storage_client.get_minio_client()
    response = None
    try:
        with storage_client.storage_errors("discussion_picture_get"):
            response = client.get_object(settings.collaboration_pictures_bucket, rendition["key"])
            data = response.read(rendition["size"] + 1)
    except S3Error:
        discussions.error(503, "picture_storage_unavailable")
    finally:
        if response is not None:
            response.close()
            response.release_conn()
    if len(data) != rendition["size"] or hashlib.sha256(data).hexdigest() != rendition["sha256"]:
        discussions.error(503, "picture_content_unavailable")
    return data


@transactional
def content(db, token, share_id, entry_id, picture_id, variant):
    ctx = discussions.context(db, token, share_id, entry_id)
    picture = picture_for(db, ctx, picture_id)
    readable(db, ctx, picture)
    rendition = (picture.renditions or {}).get(variant)
    if rendition is None:
        discussions.error(409, "picture_not_verified")
    db.commit()
    if not download_slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "picture_capacity"}, headers={"Retry-After": "2"})
    try:
        data = get_rendition(rendition)
        ctx = discussions.context(db, token, share_id, entry_id)
        picture = picture_for(db, ctx, picture_id)
        readable(db, ctx, picture)
        if (picture.renditions or {}).get(variant) != rendition:
            discussions.error(409, "picture_changed")
        db.commit()
        return data, rendition["mime"]
    finally:
        download_slots.release()

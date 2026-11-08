"""Bounded, read-only portable discussion snapshots (not service backups).

The normal actor/session/share read locks fence discussion and permission writers
while a complete scope is encoded. No object-store I/O occurs under these locks.
Removed bodies are excluded even for authors and moderators.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.api import deps
from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as schema
from app.services import (
    base_discussions,
    collaboration_auth,
    discussion_pictures,
    discussion_reattachment,
    managed_service,
)
from app.services import discussion_service as discussions
from app.services.discussion_search import read_predicate

MAX_RECORDS = 5000
MAX_BYTES = 8 * 1024 * 1024
MAX_SCANNED = 10000
MAX_SECONDS = 10
MAX_CATALOG_SCOPES = 256
slots = threading.BoundedSemaphore(2)


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_export_enabled
    )


class Archive:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.records = []
        self.bytes = 0
        self.scanned = 0
        self.people = set()
        self.pictures = set()

    def scan(self):
        self.scanned += 1
        if self.scanned > MAX_SCANNED or time.monotonic() > self.deadline:
            discussions.error(413, "discussion_export_limit")

    def append(self, kind, **value):
        self.scan()
        row = jsonable_encoder({"type": kind, **value})
        self.bytes += len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2
        if len(self.records) >= MAX_RECORDS or self.bytes > MAX_BYTES:
            discussions.error(413, "discussion_export_limit")
        self.records.append(row)
        for field in ("author_id", "actor_id"):
            if row.get(field):
                self.people.add(uuid.UUID(row[field]))

    def body(self, value):
        try:
            body = schema.MessageBody.model_validate(value)
        except ValidationError:
            discussions.error(500, "invalid_stored_body")
        self.pictures.update(body.picture_ids())
        self.people.update(body.mentioned_users())
        return body.model_dump(mode="json")


def snapshot(db, token, share_id, entry_id, *, view_id=None):
    return _read(db, lambda: _snapshot(db, token, share_id, entry_id, view_id))


def catalog(db, token, share_id):
    return _read(db, lambda: _catalog(db, token, share_id))


def _read(db, work):
    if not enabled():
        discussions.error(404, "discussion_export_disabled")
    discussions.require_clean_session(db)
    if not slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "discussion_export_busy"}, headers={"Retry-After": "2"})
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
        result = work()
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        discussions.error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        discussions.error(500, "discussion_storage_error")
    except Exception:
        db.rollback()
        raise
    finally:
        slots.release()


def _snapshot(db, token, share_id, entry_id, view_id):
    ctx = discussions.context(db, token, share_id, entry_id)
    discussions.requested_view(db, ctx, view_id)
    scope = discussions.scope_for(db, ctx)
    archive = Archive()
    if scope is not None:
        query = select(m.DiscussionThread).where(
            m.DiscussionThread.scope_id == scope.id,
            m.DiscussionThread.deleted.is_(False),
        )
        if view_id is not None:
            query = query.join(
                m.DiscussionAnchorRevision,
                and_(
                    m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                    m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
                ),
            ).where(m.DiscussionAnchorRevision.payload["view_id"].as_string() == str(view_id))
        for thread in db.scalars(query.order_by(m.DiscussionThread.created_sequence)).yield_per(50):
            archive.scan()
            if base_discussions.can_read_thread(db, ctx, thread):
                _thread(db, ctx, thread, archive)
        _pictures(db, ctx, scope, archive)
    # IDs are portable references; names/emails and a participant directory are
    # deliberately not introduced by this archive endpoint.
    for user_id in sorted(archive.people):
        archive.append("identity", id=str(user_id))
    value = {
        "format": "team-relay-discussion-export",
        "version": 1,
        "export_id": str(uuid.uuid4()),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "exporter_id": str(ctx.user.id),
        "share_id": str(ctx.share.id),
        "scope_id": str(scope.id) if scope else None,
        "entry": {
            "id": ctx.entry.id,
            "path": ctx.entry.path,
            "kind": discussions.scope_kind(ctx),
            "generation": str(ctx.entry.collaboration_generation)
            if ctx.entry.collaboration_generation
            else None,
        },
        "view_id": str(view_id) if view_id else None,
        "collaboration_epoch": str(ctx.share.collaboration_epoch)
        if ctx.share.collaboration_epoch
        else None,
        "sequence": ctx.share.collaboration_sequence,
        "includes": {
            "message_history": True,
            "anchor_history": True,
            "picture_bytes": False,
            "removed_content": False,
        },
        "records": archive.records,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        discussions.error(413, "discussion_export_limit")
    return encoded


def _thread(db, ctx, thread, archive):
    archive.append("thread", **discussions.thread_data(thread))
    for row in db.scalars(
        select(m.DiscussionAnchorRevision)
        .where(m.DiscussionAnchorRevision.thread_id == thread.id)
        .order_by(m.DiscussionAnchorRevision.revision)
    ).yield_per(50):
        anchor = discussion_reattachment.stored(row.payload)
        available = True
        if isinstance(anchor, schema.BaseAnchor):
            try:
                base_discussions.validate(db, ctx, anchor)
            except HTTPException as exc:
                if exc.status_code not in {403, 404}:
                    raise
                available = False
        archive.append(
            "anchor_revision",
            thread_id=str(thread.id),
            revision=row.revision,
            actor_id=str(row.actor_id) if row.actor_id else None,
            created_at=discussions.utc_timestamp(row.created_at),
            anchor=anchor.model_dump(mode="json") if available else None,
            unavailable=not available,
        )
    for message in db.scalars(
        select(m.DiscussionMessage)
        .where(m.DiscussionMessage.thread_id == thread.id)
        .order_by(m.DiscussionMessage.created_sequence)
    ).yield_per(50):
        archive.append(
            "message",
            id=str(message.id),
            thread_id=str(thread.id),
            author_id=str(message.author_id) if message.author_id else None,
            revision=message.revision,
            deleted=message.deleted,
            created_sequence=message.created_sequence,
            created_at=discussions.utc_timestamp(message.created_at),
            edited_at=discussions.utc_timestamp(message.edited_at),
        )
    for revision in db.scalars(
        select(m.DiscussionMessageRevision)
        .join(m.DiscussionMessage, m.DiscussionMessage.id == m.DiscussionMessageRevision.message_id)
        .where(m.DiscussionMessage.thread_id == thread.id, m.DiscussionMessage.deleted.is_(False))
        .order_by(m.DiscussionMessage.created_sequence, m.DiscussionMessageRevision.revision)
    ).yield_per(50):
        archive.append(
            "message_revision",
            message_id=str(revision.message_id),
            revision=revision.revision,
            body=archive.body(revision.body) if revision.body is not None else None,
            actor_id=str(revision.actor_id) if revision.actor_id else None,
            created_at=discussions.utc_timestamp(revision.created_at),
        )
    # Preserve the same aggregate visibility as ordinary message reads. Fetch
    # all visible reactions for this thread together, not once per message.
    for message_id, emoji, total, mine in db.execute(
        select(
            m.DiscussionReaction.message_id,
            m.DiscussionReaction.emoji,
            func.count(),
            func.sum(case((m.DiscussionReaction.user_id == ctx.user.id, 1), else_=0)),
        )
        .join(m.DiscussionMessage, m.DiscussionMessage.id == m.DiscussionReaction.message_id)
        .where(m.DiscussionMessage.thread_id == thread.id, m.DiscussionMessage.deleted.is_(False))
        .group_by(m.DiscussionReaction.message_id, m.DiscussionReaction.emoji)
        .order_by(m.DiscussionReaction.message_id, m.DiscussionReaction.emoji)
    ).yield_per(50):
        archive.append(
            "reaction", message_id=str(message_id), emoji=emoji, count=total, mine=bool(mine)
        )


def _pictures(db, ctx, scope, archive):
    for picture_id in sorted(archive.pictures):
        picture = db.get(m.DiscussionPicture, picture_id)
        # Only metadata from visible exported bodies, never staging/orphan rows
        # or pictures retained exclusively by deleted content.
        available = (
            picture is not None and picture.scope_id == scope.id and picture.state == "attached"
        )
        if available:
            discussion_pictures.readable(db, ctx, picture)
        archive.append(
            "picture",
            picture_id=str(picture_id),
            available=available,
            metadata=discussion_pictures.public_data(picture) if available else None,
        )


def _catalog(db, token, share_id):
    """An atomic, bounded scope inventory; per-scope exports reauthorize later.

    Only scopes with an actually readable, nonremoved thread are discoverable.
    Paths/view names are not returned for a denied Bases record/dependency. We
    deliberately do not promise that later scope captures share one sequence.
    """
    db.expire_all()
    user = collaboration_auth.lock_actor(db, token, require_session=True)
    share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update(read=True))
    if not share or not share.managed:
        discussions.error(404, "target_unavailable")
    moderate = user.is_admin or share.owner_user_id == user.id
    if (
        not moderate
        and db.scalar(
            select(m.ShareMember.id)
            .where(m.ShareMember.share_id == share.id, m.ShareMember.user_id == user.id)
            .with_for_update(read=True)
        )
        is None
    ):
        discussions.error(404, "target_unavailable")
    deps._get_user_from_token(db, token)
    rules = managed_service.rules_for(db, share, user)
    query = (
        select(m.DiscussionThread, m.DiscussionScope, m.ManagedEntry, m.DiscussionAnchorRevision)
        .join(m.DiscussionScope, m.DiscussionScope.id == m.DiscussionThread.scope_id)
        .join(
            m.ManagedEntry,
            and_(
                m.ManagedEntry.id == m.DiscussionScope.entry_id,
                m.ManagedEntry.share_id == share.id,
            ),
        )
        .join(
            m.DiscussionAnchorRevision,
            and_(
                m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
            ),
        )
        .where(
            m.DiscussionScope.share_id == share.id,
            m.ManagedEntry.deleted.is_(False),
            m.DiscussionThread.deleted.is_(False),
            read_predicate(share, user, rules, m.ManagedEntry.path),
        )
        .order_by(m.DiscussionThread.created_sequence)
    )
    budget, found = Archive(), {}
    for thread, scope, entry, revision in db.execute(query).yield_per(50):
        budget.scan()
        ctx = discussions.Context(user, share, entry, moderate)
        if entry.record.get("type") not in {"markdown", "image", "file"} or (
            scope.kind != discussions.scope_kind(ctx)
        ):
            continue
        view_id = revision.payload.get("view_id") if scope.kind == "base" else None
        key = (entry.id, view_id)
        if key in found:
            continue
        if not base_discussions.can_read_thread(db, ctx, thread):
            continue
        view = None
        if scope.kind == "base":
            # can_read_thread verified this exact view/definition lineage.
            view = db.get(m.BaseView, uuid.UUID(view_id))
        if len(found) >= MAX_CATALOG_SCOPES:
            discussions.error(413, "discussion_export_limit")
        found[key] = {
            "scope_id": str(scope.id),
            "entry_id": entry.id,
            "path": entry.path,
            "kind": scope.kind,
            "view_id": view_id,
            "view_name": view.descriptor.get("name") if view else None,
            "view_state": view.state if view else None,
        }
    value = {
        "format": "team-relay-discussion-export-catalog",
        "version": 1,
        "catalog_id": str(uuid.uuid4()),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "exporter_id": str(user.id),
        "share_id": str(share.id),
        "collaboration_epoch": str(share.collaboration_epoch)
        if share.collaboration_epoch
        else None,
        "sequence": share.collaboration_sequence,
        "scopes": [found[key] for key in sorted(found, key=lambda item: (item[0], item[1] or ""))],
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES or time.monotonic() > budget.deadline:
        discussions.error(413, "discussion_export_limit")
    return encoded

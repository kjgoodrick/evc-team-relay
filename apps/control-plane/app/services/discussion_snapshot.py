"""A bounded, consistent current-state baseline for discussion synchronization.

Actor/session/share locks cover the complete response and its catch-up cursor.
No historical body, object-store I/O or retained database snapshot is involved.
Event pruning stays disabled until retention and compatible-client admission are
connected; a snapshot response by itself does not authorize pruning history.
"""

import json
import threading
import time

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as schema
from app.services import base_discussions, discussion_reattachment
from app.services import discussion_service as discussions

MAX_RECORDS = 5000
MAX_SCANNED = 10000
MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 10
slots = threading.BoundedSemaphore(2)


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_snapshot_enabled
    )


class Budget:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.scanned = 0
        self.records = 0
        self.bytes = 0

    def scan(self):
        self.scanned += 1
        if self.scanned > MAX_SCANNED or time.monotonic() > self.deadline:
            discussions.error(413, "discussion_snapshot_limit")

    def add(self, value):
        self.scan()
        self.records += 1
        self.bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 2
        if self.records > MAX_RECORDS or self.bytes > MAX_BYTES:
            discussions.error(413, "discussion_snapshot_limit")


def snapshot(db, token, share_id, entry_id, *, view_id=None):
    if not enabled():
        discussions.error(404, "discussion_snapshot_disabled")
    discussions.require_clean_session(db)
    if not slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "discussion_snapshot_busy"}, headers={"Retry-After": "2"})
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
        result = _snapshot(db, token, share_id, entry_id, view_id)
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
    budget = Budget()
    threads = []
    if scope is not None:
        query = (
            select(m.DiscussionThread, m.DiscussionAnchorRevision)
            .outerjoin(
                m.DiscussionAnchorRevision,
                and_(
                    m.DiscussionAnchorRevision.thread_id == m.DiscussionThread.id,
                    m.DiscussionAnchorRevision.revision == m.DiscussionThread.anchor_revision,
                ),
            )
            .where(
                m.DiscussionThread.scope_id == scope.id,
                m.DiscussionThread.deleted.is_(False),
            )
            .order_by(m.DiscussionThread.created_sequence)
        )
        if view_id is not None:
            query = query.where(
                or_(
                    m.DiscussionAnchorRevision.thread_id.is_(None),
                    m.DiscussionAnchorRevision.payload["view_id"].as_string() == str(view_id),
                )
            )
        for thread, anchor in db.execute(query).yield_per(50):
            budget.scan()
            if anchor is None:
                discussions.error(500, "invalid_stored_anchor")
            if not base_discussions.can_read_thread(db, ctx, thread):
                continue
            value = jsonable_encoder(
                {
                    "thread": discussions.thread_data(thread),
                    "anchor": discussion_reattachment.stored(anchor.payload).model_dump(
                        mode="json"
                    ),
                    "messages": [],
                }
            )
            budget.add(value)
            rows = db.execute(
                select(m.DiscussionMessage, m.DiscussionMessageRevision)
                .outerjoin(
                    m.DiscussionMessageRevision,
                    and_(
                        m.DiscussionMessageRevision.message_id == m.DiscussionMessage.id,
                        m.DiscussionMessageRevision.revision == m.DiscussionMessage.revision,
                    ),
                )
                .where(m.DiscussionMessage.thread_id == thread.id)
                .order_by(m.DiscussionMessage.created_sequence)
            ).yield_per(50)
            for batch in rows.partitions(50):
                if any(revision is None for _, revision in batch):
                    discussions.error(500, "invalid_stored_body")
                for message in discussions.message_data(db, ctx, batch):
                    if message["body"] is None and not message["deleted"]:
                        discussions.error(500, "invalid_stored_body")
                    if message["body"] is not None:
                        try:
                            message["body"] = schema.MessageBody.model_validate(
                                message["body"]
                            ).model_dump(mode="json")
                        except ValidationError:
                            discussions.error(500, "invalid_stored_body")
                    message = jsonable_encoder(message)
                    budget.add(message)
                    value["messages"].append(message)
            if not value["messages"]:
                discussions.error(500, "invalid_stored_body")
            threads.append(value)
    sequence = ctx.share.collaboration_sequence
    cursor = (
        discussions.cursor_codec()
        .encrypt(
            json.dumps(discussions.cursor_value(ctx, sequence, view_id), sort_keys=True).encode()
        )
        .decode()
    )
    result = {
        "protocol_version": 1,
        "entry_id": ctx.entry.id,
        "through_sequence": sequence,
        "restore_generation": str(ctx.share.collaboration_restore_generation)
        if ctx.share.collaboration_restore_generation
        else None,
        "threads": threads,
        "cursor": cursor,
        **({"view_id": str(view_id)} if view_id is not None else {}),
    }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        discussions.error(413, "discussion_snapshot_limit")
    return encoded

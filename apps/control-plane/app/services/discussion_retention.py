"""Bounded SQL-only purge of removed content after its recovery window.

Keep entity IDs, revisions, tombstone metadata and operation receipts. Expire a
picture only when this transaction removes its last retained reference; the
existing private-picture sweep owns physical object I/O and retirement fencing.
"""

import threading
import time
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.orm import aliased

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_sessionmaker

BATCH_SECONDS = 30
PURGED_ANCHOR = {"version": 1, "type": "purged"}


class Stopped(Exception):
    pass


@dataclass
class Counts:
    messages: int = 0
    threads: int = 0
    picture_identities_expired: int = 0
    busy: int = 0
    stopped: bool = False


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def expired(row, cutoff):
    return row.deleted and row.deleted_at is not None and aware(row.deleted_at) <= cutoff


def cutoff(now):
    return now - timedelta(days=get_settings().collaboration_removed_content_retention_days)


def guard(stop, deadline):
    if (
        stop.is_set()
        or not get_settings().collaboration_removed_content_cleanup_enabled
        or time.monotonic() >= deadline
    ):
        raise Stopped


def query_limits(db):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '10s'"))


def lock_share(db, share_id):
    query_limits(db)
    return db.scalar(
        select(m.Share).where(m.Share.id == share_id).with_for_update(skip_locked=True)
    )


def purge_message(db, message, scope_id, now):
    references = m.DiscussionPictureReference
    other = aliased(references)
    pictures = db.execute(
        update(m.DiscussionPicture)
        .where(
            m.DiscussionPicture.scope_id == scope_id,
            m.DiscussionPicture.state == "attached",
            m.DiscussionPicture.id.in_(
                select(references.picture_id).where(references.message_id == message.id)
            ),
            ~select(other.picture_id)
            .where(other.picture_id == m.DiscussionPicture.id, other.message_id != message.id)
            .exists(),
        )
        .values(state="expired")
        .execution_options(synchronize_session=False)
    ).rowcount
    db.execute(delete(references).where(references.message_id == message.id))
    db.execute(
        update(m.DiscussionMessageRevision)
        .where(m.DiscussionMessageRevision.message_id == message.id)
        .values(body=None, reason=None)
        .execution_options(synchronize_session=False)
    )
    db.execute(delete(m.DiscussionReaction).where(m.DiscussionReaction.message_id == message.id))
    db.execute(
        delete(m.DiscussionSearchDocument).where(
            m.DiscussionSearchDocument.message_id == message.id
        )
    )
    message.content_purged_at = now
    db.flush()
    return pictures


def finish_thread(db, thread, now):
    if thread.content_purged_at is not None or not expired(thread, cutoff(now)):
        return False
    if (
        db.scalar(
            select(m.DiscussionMessage.id)
            .where(
                m.DiscussionMessage.thread_id == thread.id,
                m.DiscussionMessage.content_purged_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        return False
    db.execute(
        update(m.DiscussionAnchorRevision)
        .where(m.DiscussionAnchorRevision.thread_id == thread.id)
        .values(payload=PURGED_ANCHOR)
        .execution_options(synchronize_session=False)
    )
    thread.removal_reason = None
    thread.content_purged_at = now
    db.flush()
    return True


def run_batch(stop=None, *, factory=None, now=None):
    stop = stop or threading.Event()
    factory = factory or get_sessionmaker()
    now = now or security.utcnow()
    counts, deadline = Counts(), time.monotonic() + BATCH_SECONDS
    try:
        guard(stop, deadline)
        maximum = get_settings().collaboration_removed_content_cleanup_batch_size
        with factory() as db:
            query_limits(db)
            message_expired = and_(
                m.DiscussionMessage.deleted.is_(True), m.DiscussionMessage.deleted_at <= cutoff(now)
            )
            thread_expired = and_(
                m.DiscussionThread.deleted.is_(True), m.DiscussionThread.deleted_at <= cutoff(now)
            )
            # IDs only; no body/history materialization and no lock-order inversion.
            candidates = db.execute(
                select(m.DiscussionMessage.id, m.DiscussionScope.share_id)
                .join(m.DiscussionThread, m.DiscussionThread.id == m.DiscussionMessage.thread_id)
                .join(m.DiscussionScope, m.DiscussionScope.id == m.DiscussionThread.scope_id)
                .where(
                    m.DiscussionMessage.content_purged_at.is_(None),
                    or_(message_expired, thread_expired),
                )
                .order_by(m.DiscussionMessage.id)
                .limit(maximum)
            ).all()
        for message_id, share_id in candidates:
            guard(stop, deadline)
            with factory.begin() as db:
                if lock_share(db, share_id) is None:
                    counts.busy += 1
                    continue
                guard(stop, deadline)
                message = db.get(m.DiscussionMessage, message_id)
                if message is None or message.content_purged_at is not None:
                    continue
                thread = db.get(m.DiscussionThread, message.thread_id)
                scope = db.get(m.DiscussionScope, thread.scope_id) if thread is not None else None
                if scope is None or scope.share_id != share_id:
                    continue
                if thread is None or not (
                    expired(message, cutoff(now)) or expired(thread, cutoff(now))
                ):
                    continue
                picture_count = purge_message(db, message, thread.scope_id, now)
                thread_done = finish_thread(db, thread, now)
                guard(stop, deadline)
            counts.messages += 1
            counts.picture_identities_expired += picture_count
            counts.threads += int(thread_done)
        # Also finish empty threads or ones whose children were purged earlier.
        guard(stop, deadline)
        with factory() as db:
            query_limits(db)
            candidates = db.execute(
                select(m.DiscussionThread.id, m.DiscussionScope.share_id)
                .join(m.DiscussionScope, m.DiscussionScope.id == m.DiscussionThread.scope_id)
                .where(
                    m.DiscussionThread.deleted.is_(True),
                    m.DiscussionThread.deleted_at <= cutoff(now),
                    m.DiscussionThread.content_purged_at.is_(None),
                    ~select(m.DiscussionMessage.id)
                    .where(
                        m.DiscussionMessage.thread_id == m.DiscussionThread.id,
                        m.DiscussionMessage.content_purged_at.is_(None),
                    )
                    .exists(),
                )
                .order_by(m.DiscussionThread.id)
                .limit(max(0, maximum - counts.threads))
            ).all()
        for thread_id, share_id in candidates:
            guard(stop, deadline)
            with factory.begin() as db:
                if lock_share(db, share_id) is None:
                    counts.busy += 1
                    continue
                thread = db.get(m.DiscussionThread, thread_id)
                scope = db.get(m.DiscussionScope, thread.scope_id) if thread is not None else None
                if scope is None or scope.share_id != share_id:
                    continue
                done = thread is not None and finish_thread(db, thread, now)
                guard(stop, deadline)
            counts.threads += int(done)
    except Stopped:
        counts.stopped = True
    return counts

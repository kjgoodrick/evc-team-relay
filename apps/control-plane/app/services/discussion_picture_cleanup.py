"""Bounded, resumable reconciliation of the private comment-picture namespace.

Expire identities before object I/O. Every historical revision protects its
pictures, including removed messages/threads. Repeated inventories catch writes
that finish after an earlier sweep. Retired identities survive SQL cascades so a
new upload cannot race deletion by reusing an absent picture's namespace.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clients import storage_client
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_sessionmaker
from app.services.discussion_pictures import STAGING_TTL, namespace

ROOT = "discussion-pictures/"
LEASE_TTL = timedelta(seconds=120)
BATCH_SECONDS = 30
KEY = re.compile(
    r"^discussion-pictures/([0-9a-f-]{36})/([0-9a-f-]{36})/([0-9a-f-]{36})"
    r"/v1/[0-9a-f]{64}\.(png|jpg)$"
)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass(frozen=True)
class Identity:
    share: uuid.UUID
    scope: uuid.UUID
    picture: uuid.UUID
    prefix: str


def identify(key):
    match = KEY.fullmatch(key)
    if match is None:
        return None
    try:
        ids = tuple(uuid.UUID(value) for value in match.groups()[:3])
    except ValueError:
        return None
    if tuple(map(str, ids)) != match.groups()[:3]:
        return None
    return Identity(*ids, namespace(*ids))


class Stopped(Exception):
    """Lease ownership, shutdown or configuration changed; resume later."""


class UnsupportedBucket(Exception):
    """Cleanup requires its own private, unversioned picture bucket."""


@dataclass
class Counts:
    visited: int = 0
    deleted: int = 0
    deleted_bytes: int = 0
    expired: int = 0
    deferred: int = 0
    unknown: int = 0
    completed: bool = False
    claimed: bool = False


class Sweep:
    def __init__(self, factory=None, stop=None):
        self.factory = factory or get_sessionmaker()
        self.stop = stop or threading.Event()
        self.settings = get_settings()
        self.bucket = self.settings.collaboration_pictures_bucket
        self.token = uuid.uuid4()
        self.cursor = None
        self.counts = Counts()

    def configured(self):
        return (
            not self.stop.is_set()
            and self.settings.collaboration_picture_cleanup_enabled
            and self.settings.collaboration_pictures_bucket == self.bucket
        )

    def claim(self):
        if not self.configured():
            return False
        if self.bucket == self.settings.minio_bucket:
            raise UnsupportedBucket
        now = security.utcnow()
        try:
            with self.factory.begin() as db:
                row = db.scalar(
                    select(m.DiscussionPictureSweep)
                    .where(m.DiscussionPictureSweep.bucket == self.bucket)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    # A concurrent inserter/locked row loses at the unique key.
                    # Avoid waiting on a known locked row in the usual case.
                    if db.get(m.DiscussionPictureSweep, self.bucket) is not None:
                        return False
                    row = m.DiscussionPictureSweep(bucket=self.bucket, next_run_at=now)
                    db.add(row)
                elif aware(row.next_run_at) > now or (
                    row.lease_until is not None and aware(row.lease_until) > now
                ):
                    return False
                row.lease_token, row.lease_until = self.token, now + LEASE_TTL
                if row.cycle_started_at is None:
                    row.cycle_started_at = now
                self.cursor = row.cursor
            self.counts.claimed = True
            return True
        except IntegrityError:
            return False

    def guard(self, db):
        if not self.configured():
            raise Stopped
        row = db.scalar(
            select(m.DiscussionPictureSweep)
            .where(m.DiscussionPictureSweep.bucket == self.bucket)
            .with_for_update(skip_locked=True)
        )
        now = security.utcnow()
        if (
            row is None
            or row.lease_token != self.token
            or row.lease_until is None
            or aware(row.lease_until) <= now
        ):
            raise Stopped
        row.lease_until = now + LEASE_TTL
        return row

    def heartbeat(self):
        with self.factory.begin() as db:
            self.guard(db)

    def advance(self, key):
        with self.factory.begin() as db:
            self.guard(db).cursor = key
        self.cursor = key

    def release(self, *, complete=False, error=None):
        # Release only our own lease, including after cooperative shutdown. A
        # stale worker must not clear or advance a successor's progress.
        with self.factory.begin() as db:
            row = db.scalar(
                select(m.DiscussionPictureSweep)
                .where(m.DiscussionPictureSweep.bucket == self.bucket)
                .with_for_update(skip_locked=True)
            )
            if row is None or row.lease_token != self.token:
                return
            now = security.utcnow()
            if complete:
                # Observations with no objects left need not accumulate. Retired
                # identities are permanent guards and are never pruned here.
                stale = db.scalars(
                    select(m.DiscussionPictureRetirement)
                    .where(
                        m.DiscussionPictureRetirement.bucket == self.bucket,
                        m.DiscussionPictureRetirement.retired_at.is_(None),
                        m.DiscussionPictureRetirement.last_seen_at < row.cycle_started_at,
                    )
                    .limit(self.settings.collaboration_picture_cleanup_batch_size)
                ).all()
                for observation in stale:
                    db.delete(observation)
                row.cursor = row.cycle_started_at = None
                row.last_completed_at = now
            delay = (
                self.settings.collaboration_picture_cleanup_cycle_seconds
                if complete
                else self.settings.collaboration_picture_cleanup_interval_seconds
            )
            row.next_run_at = now + timedelta(seconds=delay)
            row.lease_token = row.lease_until = None
            row.last_error = error

    @staticmethod
    def lock_share(db, share_id):
        row = db.scalar(
            select(m.Share).where(m.Share.id == share_id).with_for_update(skip_locked=True)
        )
        # A busy live share is not a missing share. Do not block foreground work.
        return row is not None or db.get(m.Share, share_id) is None

    @staticmethod
    def referenced(db, picture_id):
        return (
            db.scalar(
                select(m.DiscussionPictureReference.picture_id)
                .where(m.DiscussionPictureReference.picture_id == picture_id)
                .limit(1)
            )
            is not None
        )

    def expire_pending(self, deadline):
        now = security.utcnow()
        with self.factory() as db:
            candidates = db.execute(
                select(m.DiscussionPicture.id, m.DiscussionScope.share_id)
                .join(m.DiscussionScope)
                .where(
                    m.DiscussionPicture.state.in_(["staged", "verified"]),
                    m.DiscussionPicture.expires_at <= now,
                    ~select(m.DiscussionPictureReference.picture_id)
                    .where(m.DiscussionPictureReference.picture_id == m.DiscussionPicture.id)
                    .exists(),
                )
                .order_by(m.DiscussionPicture.expires_at, m.DiscussionPicture.id)
                .limit(self.settings.collaboration_picture_cleanup_batch_size)
            ).all()
        for picture_id, share_id in candidates:
            if time.monotonic() >= deadline:
                break
            with self.factory.begin() as db:
                self.guard(db)
                if not self.lock_share(db, share_id):
                    continue
                picture = db.get(m.DiscussionPicture, picture_id)
                if (
                    picture is not None
                    and picture.state in {"staged", "verified"}
                    and aware(picture.expires_at) <= now
                    and not self.referenced(db, picture.id)
                ):
                    picture.state = "expired"
                    self.counts.expired += 1

    def classify(self, obj, identity):
        """Return True only after committing an irreversible deletion guard."""
        now = security.utcnow()
        with self.factory.begin() as db:
            self.guard(db)
            if not self.lock_share(db, identity.share):
                self.counts.deferred += 1
                return False
            picture = db.get(m.DiscussionPicture, identity.picture)
            scope = db.get(m.DiscussionScope, identity.scope)
            matches = (
                picture is not None
                and scope is not None
                and picture.scope_id == scope.id
                and scope.share_id == identity.share
            )
            observation = db.get(m.DiscussionPictureRetirement, (self.bucket, identity.prefix))
            if observation is not None:
                observation.last_seen_at = now
            if picture is not None and self.referenced(db, picture.id):
                if not matches:
                    # An inconsistent import must be repaired explicitly; never
                    # guess which namespace a historical reference intended.
                    return False
                if observation is not None and observation.retired_at is None:
                    db.delete(observation)
                # Even inconsistent/restored rows cannot lose referenced bytes.
                if picture.state != "attached" or not isinstance(picture.renditions, dict):
                    return False
                renditions = picture.renditions
                if set(renditions) != {"content", "thumbnail"}:
                    return False
                keys = []
                for value in renditions.values():
                    key = value.get("key") if isinstance(value, dict) else None
                    if not isinstance(key, str) or identify(key) != identity:
                        return False
                    keys.append(key)
                # A losing concurrent encoder can leave extra immutable objects.
                # No live upload can change attached metadata; old extras are safe.
                return (
                    obj.object_name not in keys
                    and obj.last_modified is not None
                    and aware(obj.last_modified) + STAGING_TTL <= now
                )
            if matches and picture.state in {"staged", "verified"}:
                if aware(picture.expires_at) > now:
                    if observation is not None and observation.retired_at is None:
                        db.delete(observation)
                    return False
                picture.state = "expired"
                self.counts.expired += 1
            if observation is None:
                observation = m.DiscussionPictureRetirement(
                    bucket=self.bucket,
                    prefix=identity.prefix,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(observation)
            if observation.retired_at is not None:
                return True
            if not (matches and picture.state == "expired"):
                cutoff = now - timedelta(
                    days=self.settings.collaboration_picture_orphan_retention_days
                )
                if aware(observation.first_seen_at) > cutoff:
                    return False
            observation.retired_at = now
            if matches:
                picture.state = "expired"
            return True

    def run(self):
        if not self.claim():
            return self.counts
        error = None
        try:
            deadline = time.monotonic() + BATCH_SECONDS
            self.expire_pending(deadline)
            self.heartbeat()
            client = storage_client.get_minio_client()
            # Delete markers in a versioned bucket would not reclaim bytes. Fail
            # closed instead of reporting successful physical reclamation.
            versioning = client.get_bucket_versioning(self.bucket)
            if versioning.status is not None:
                raise UnsupportedBucket
            objects = iter(
                client.list_objects(
                    self.bucket, prefix=ROOT, recursive=True, start_after=self.cursor
                )
            )
            while (
                self.counts.visited < self.settings.collaboration_picture_cleanup_batch_size
                and time.monotonic() < deadline
            ):
                self.heartbeat()
                obj = next(objects, None)
                if obj is None:
                    self.counts.completed = True
                    break
                key = obj.object_name
                if not isinstance(key, str) or (self.cursor is not None and key <= self.cursor):
                    raise ValueError("invalid_inventory_order")
                self.counts.visited += 1
                identity = identify(key)
                if identity is None:
                    self.counts.unknown += 1
                elif self.classify(obj, identity):
                    # No transaction/authorization lock spans storage operations.
                    self.heartbeat()
                    client.remove_object(self.bucket, key)
                    self.counts.deleted += 1
                    self.counts.deleted_bytes += obj.size or 0
                self.advance(key)
            return self.counts
        except Stopped:
            return self.counts
        except Exception as exc:
            # Never retain raw S3 keys, URLs, credentials or exception messages.
            error = type(exc).__name__[:64]
            raise
        finally:
            self.release(complete=self.counts.completed, error=error)


def run_batch(stop=None):
    return Sweep(stop=stop).run()

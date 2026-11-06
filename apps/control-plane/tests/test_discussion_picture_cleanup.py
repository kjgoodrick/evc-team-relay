import asyncio
import importlib
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_discussion_pictures import publish, raster, stage, upload
from test_discussion_pictures import storage as picture_storage
from test_discussions import body, envelope, send
from test_discussions import world as discussion_world

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_picture_cleanup as cleanup
from app.services import discussion_pictures as pictures
from app.workers import discussion_picture_worker as worker

world = discussion_world
storage = picture_storage


@pytest.fixture
def maintenance(storage, db_connection, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "collaboration_picture_cleanup_enabled", True)
    sessions = []

    class TrackedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    factory = sessionmaker(
        bind=db_connection,
        class_=TrackedSession,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )

    class Inventory:
        modified = {}
        before_remove = None
        deleted = []
        versioning = None
        list_starts = []

        def __getattr__(self, name):
            return getattr(storage, name)

        def unlocked(self):
            assert not any(s.in_transaction() for s in sessions), "SQL lock during object I/O"

        def get_bucket_versioning(self, bucket):
            self.unlocked()
            return SimpleNamespace(status=self.versioning)

        def list_objects(self, bucket, *, prefix, recursive, start_after):
            self.unlocked()
            assert recursive and prefix == cleanup.ROOT
            self.list_starts.append(start_after)
            for key in sorted(storage.objects):
                self.unlocked()
                if key.startswith(prefix) and (start_after is None or key > start_after):
                    yield SimpleNamespace(
                        object_name=key,
                        size=len(storage.objects[key]),
                        last_modified=self.modified.get(key, security.utcnow()),
                    )

        def remove_object(self, bucket, key):
            self.unlocked()
            if self.before_remove:
                self.before_remove(key)
            storage.objects.pop(key, None)
            self.deleted.append(key)

    inventory = Inventory()
    monkeypatch.setattr(cleanup.storage_client, "get_minio_client", lambda: inventory)
    monkeypatch.setattr(cleanup, "get_sessionmaker", lambda: factory)
    return SimpleNamespace(store=inventory, factory=factory, settings=settings)


def wake(db, *, restart=False):
    row = db.get(m.DiscussionPictureSweep, get_settings().collaboration_pictures_bucket)
    if row:
        row.next_run_at = security.utcnow() - timedelta(seconds=1)
        if restart:
            row.cursor = row.cycle_started_at = None
        db.commit()


def expire(db, pid):
    picture = db.get(m.DiscussionPicture, uuid.UUID(pid))
    picture.expires_at = security.utcnow() - timedelta(seconds=1)
    db.commit()


def orphan_key(w, *, scope=None, pid=None):
    return pictures.namespace(w.share.id, scope or uuid.uuid4(), pid or uuid.uuid4()) + (
        "v1/" + "1" * 64 + ".png"
    )


def test_expiry_releases_quota_without_waiting_for_worker(
    client, db_session, world, storage, monkeypatch
):
    monkeypatch.setattr(pictures, "STAGING_COUNT", 1)
    data = raster()
    original, _ = stage(client, world, data)
    stage(client, world, data, expected=409)
    expire(db_session, original["picture_id"])
    replacement, _ = stage(client, world, data)
    assert replacement["picture_id"] != original["picture_id"]
    stage(client, world, data, picture_id=original["picture_id"], expected=410)


def test_expired_identity_precedes_deletion_and_late_put_is_reconciled(
    client, db_session, world, maintenance
):
    pid, _ = upload(client, world, raster(size=(700, 400)))
    expire(db_session, pid)
    original = dict(maintenance.store.objects)

    def assert_retired(key):
        with maintenance.factory() as db:
            picture = db.get(m.DiscussionPicture, uuid.UUID(pid))
            assert picture.state == "expired"
            identity = cleanup.identify(key)
            retired = db.get(
                m.DiscussionPictureRetirement,
                (maintenance.settings.collaboration_pictures_bucket, identity.prefix),
            )
            assert retired.retired_at is not None

    maintenance.store.before_remove = assert_retired
    counts = cleanup.run_batch()
    assert counts.expired == 1 and counts.deleted == len(original) and counts.completed
    assert not maintenance.store.objects
    # A PUT started before expiry can finish after every prior object was removed.
    maintenance.store.objects.update(original)
    wake(db_session)
    assert cleanup.run_batch().deleted == len(original)
    assert not maintenance.store.objects
    upload(client, world, raster(), picture_id=pid, expected=410)


def test_metadata_expiry_does_not_require_an_object_or_enabled_creation(
    client, db_session, world, maintenance, monkeypatch
):
    payload, _ = stage(client, world, raster())
    expire(db_session, payload["picture_id"])
    monkeypatch.setattr(maintenance.settings, "collaboration_pictures_enabled", False)
    counts = cleanup.run_batch()
    assert counts.expired == 1 and counts.deleted == 0
    db_session.expire_all()
    assert db_session.get(m.DiscussionPicture, uuid.UUID(payload["picture_id"])).state == "expired"


def test_every_history_revision_protects_pictures_after_edit_delete_and_remove(
    client, db_session, world, maintenance
):
    pid, _ = upload(client, world, raster())
    command, _ = publish(client, world, pid)
    ids = command["command"]
    send(
        client,
        world,
        envelope(
            world,
            dict(
                type="message.edit",
                thread_id=ids["thread_id"],
                message_id=ids["message_id"],
                expected_revision=1,
                body=body("Text only now"),
            ),
        ),
    )
    send(
        client,
        world,
        envelope(
            world,
            dict(
                type="message.delete",
                thread_id=ids["thread_id"],
                message_id=ids["message_id"],
                expected_revision=2,
            ),
        ),
    )
    send(
        client,
        world,
        envelope(
            world,
            dict(
                type="thread.remove",
                thread_id=ids["thread_id"],
                expected_state_revision=1,
                reason="Removed",
            ),
        ),
        actor="owner",
    )
    expire(db_session, pid)
    before = dict(maintenance.store.objects)
    assert cleanup.run_batch().deleted == 0
    assert maintenance.store.objects == before
    db_session.expire_all()
    assert db_session.get(m.DiscussionPicture, uuid.UUID(pid)).state == "attached"


def test_unreferenced_namespace_waits_from_first_observation_not_object_age(
    db_session, world, maintenance
):
    key = orphan_key(world)
    maintenance.store.objects[key] = b"old abandoned pixels"
    maintenance.store.modified[key] = security.utcnow() - timedelta(days=300)
    assert cleanup.run_batch().deleted == 0
    observation = db_session.scalar(select(m.DiscussionPictureRetirement))
    assert observation.retired_at is None
    wake(db_session)
    assert cleanup.run_batch().deleted == 0
    observation.first_seen_at = security.utcnow() - timedelta(days=31)
    db_session.commit()
    wake(db_session)
    assert cleanup.run_batch().deleted == 1
    db_session.expire_all()
    assert db_session.scalar(select(m.DiscussionPictureRetirement)).retired_at is not None


def test_missing_picture_id_cannot_be_recreated_after_retirement(
    client, db_session, world, maintenance
):
    payload, _ = stage(client, world, raster())
    picture = db_session.get(m.DiscussionPicture, uuid.UUID(payload["picture_id"]))
    key = orphan_key(world, scope=picture.scope_id, pid=picture.id)
    db_session.delete(picture)
    db_session.commit()
    maintenance.store.objects[key] = b"orphan"
    cleanup.run_batch()
    observation = db_session.scalar(select(m.DiscussionPictureRetirement))
    observation.first_seen_at = security.utcnow() - timedelta(days=31)
    db_session.commit()

    def attempt_reuse(_key):
        _, rejected = stage(client, world, raster(), picture_id=payload["picture_id"], expected=409)
        assert "identifier_unavailable" in str(rejected)

    maintenance.store.before_remove = attempt_reuse
    wake(db_session)
    assert cleanup.run_batch().deleted == 1
    # Empty subsequent inventories do not prune the permanent identity guard.
    wake(db_session)
    cleanup.run_batch()
    stage(client, world, raster(), picture_id=payload["picture_id"], expected=409)


def test_observed_orphan_referenced_again_is_kept_and_observation_cleared(
    client, db_session, world, maintenance
):
    pid, _ = upload(client, world, raster())
    picture = db_session.get(m.DiscussionPicture, uuid.UUID(pid))
    picture.state = "attached"  # Recovery/import before the reference is restored.
    db_session.commit()
    assert cleanup.run_batch().deleted == 0
    assert db_session.scalar(select(m.DiscussionPictureRetirement)) is not None
    publish(client, world, pid)
    wake(db_session)
    assert cleanup.run_batch().deleted == 0
    db_session.expire_all()
    assert db_session.scalar(select(m.DiscussionPictureRetirement)) is None


def test_extra_encoder_outputs_age_out_but_canonical_renditions_stay(
    client, db_session, world, maintenance
):
    pid, _ = upload(client, world, raster(size=(700, 400)))
    publish(client, world, pid)
    canonical = set(maintenance.store.objects)
    prefix = next(iter(canonical)).rsplit("/", 1)[0] + "/"
    extra = prefix + "f" * 64 + ".png"
    maintenance.store.objects[extra] = b"unused encoder output"
    assert cleanup.run_batch().deleted == 0
    maintenance.store.modified[extra] = security.utcnow() - timedelta(days=2)
    wake(db_session)
    assert cleanup.run_batch().deleted == 1
    assert set(maintenance.store.objects) == canonical


def test_failed_delete_preserves_cursor_and_retries_without_losing_history(
    client, db_session, world, maintenance
):
    pid, _ = upload(client, world, raster(size=(700, 400)))
    expire(db_session, pid)
    keys = sorted(maintenance.store.objects)

    def fail_second(key):
        if key == keys[1]:
            raise OSError("do not log this private key or credential")

    maintenance.store.before_remove = fail_second
    with pytest.raises(OSError):
        cleanup.run_batch()
    row = db_session.get(
        m.DiscussionPictureSweep, maintenance.settings.collaboration_pictures_bucket
    )
    assert row.cursor == keys[0] and row.last_error == "OSError" and row.lease_token is None
    maintenance.store.before_remove = None
    wake(db_session)
    assert cleanup.run_batch().deleted == 1
    assert not maintenance.store.objects


def test_inventory_batch_bound_and_checkpoint_resume(db_session, world, maintenance, monkeypatch):
    monkeypatch.setattr(maintenance.settings, "collaboration_picture_cleanup_batch_size", 2)
    keys = sorted(orphan_key(world) for _ in range(5))
    maintenance.store.objects.update({key: b"unknown owner" for key in keys})
    assert cleanup.run_batch().visited == 2
    wake(db_session)
    assert cleanup.run_batch().visited == 2
    wake(db_session)
    counts = cleanup.run_batch()
    assert counts.visited == 1 and counts.completed
    assert maintenance.store.list_starts == [None, keys[1], keys[3]]
    row = db_session.get(
        m.DiscussionPictureSweep, maintenance.settings.collaboration_pictures_bucket
    )
    db_session.refresh(row)
    assert row.cursor is None and row.last_completed_at is not None
    assert not cleanup.run_batch().claimed  # Completed-cycle cooldown.


def test_unknown_key_versions_are_never_deleted(db_session, world, maintenance):
    valid = orphan_key(world)
    keys = [
        valid.replace("/v1/", "/v2/"),
        valid.replace(".png", ".svg"),
        "discussion-pictures/other",
    ]
    maintenance.store.objects.update({key: b"unknown" for key in keys})
    counts = cleanup.run_batch()
    assert counts.unknown == 3 and counts.deleted == 0
    assert db_session.scalar(select(m.DiscussionPictureRetirement)) is None


def test_only_lease_owner_can_advance_or_release_and_crash_is_recoverable(db_session, maintenance):
    first, other = cleanup.Sweep(), cleanup.Sweep()
    assert first.claim() and not other.claim()
    first.advance("discussion-pictures/progress")
    row = db_session.get(
        m.DiscussionPictureSweep, maintenance.settings.collaboration_pictures_bucket
    )
    row.lease_until = security.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert other.claim() and other.cursor == "discussion-pictures/progress"
    with pytest.raises(cleanup.Stopped):
        first.advance("stale overwrite")
    first.release(complete=True)
    db_session.expire_all()
    row = db_session.get(
        m.DiscussionPictureSweep, maintenance.settings.collaboration_pictures_bucket
    )
    assert row.lease_token == other.token and row.cursor == other.cursor
    other.release()


@pytest.mark.parametrize("mode", ["disabled", "stop", "bucket_change"])
def test_change_after_retirement_prevents_storage_delete(
    client, db_session, world, maintenance, monkeypatch, mode
):
    pid, _ = upload(client, world, raster())
    expire(db_session, pid)
    sweep = cleanup.Sweep()
    classify = sweep.classify

    def changed(*args):
        result = classify(*args)
        if mode == "disabled":
            monkeypatch.setattr(
                maintenance.settings, "collaboration_picture_cleanup_enabled", False
            )
        elif mode == "bucket_change":
            monkeypatch.setattr(
                maintenance.settings, "collaboration_pictures_bucket", "different-bucket"
            )
        else:
            sweep.stop.set()
        return result

    monkeypatch.setattr(sweep, "classify", changed)
    assert sweep.run().deleted == 0
    assert maintenance.store.objects


@pytest.mark.parametrize("status", ["Enabled", "Suspended"])
def test_versioned_bucket_is_rejected(maintenance, status):
    maintenance.store.versioning = status
    with pytest.raises(cleanup.UnsupportedBucket):
        cleanup.run_batch()
    assert not maintenance.store.deleted


def test_general_attachment_bucket_is_rejected(maintenance, monkeypatch):
    monkeypatch.setattr(
        maintenance.settings, "collaboration_pictures_bucket", maintenance.settings.minio_bucket
    )
    with pytest.raises(cleanup.UnsupportedBucket):
        cleanup.run_batch()


def test_cancelled_async_worker_waits_for_running_storage_job(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work(stop):
        started.set()
        assert release.wait(5)
        assert stop.is_set()
        finished.set()

    monkeypatch.setattr(worker, "run_batch", work)

    async def exercise():
        stop = threading.Event()
        task = asyncio.create_task(worker.finish_batch(stop))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert stop.is_set() and not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(exercise())


def test_cleanup_schema_upgrade_downgrade_and_guards_survive_cascades():
    migration = importlib.import_module("app.db.migrations.versions.202611060002_picture_cleanup")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = sa.inspect(connection)
                assert set(inspector.get_table_names()) == {
                    "discussion_picture_sweeps",
                    "discussion_picture_retirements",
                }
                assert not inspector.get_foreign_keys("discussion_picture_retirements")
                migration.downgrade()
                assert not sa.inspect(connection).get_table_names()
                migration.upgrade()
    finally:
        engine.dispose()

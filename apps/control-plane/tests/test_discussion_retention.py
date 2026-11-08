import asyncio
import importlib
import threading
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker
from test_base_discussions import base as base_fixture
from test_base_discussions import command, get
from test_base_discussions import targets as targets_fixture
from test_discussion_export import archive, records
from test_discussion_inbox import listing, mention
from test_discussion_picture_cleanup import maintenance as picture_maintenance_fixture
from test_discussion_pictures import picture_body, publish, raster, upload
from test_discussion_pictures import storage as picture_storage
from test_discussions import base as route
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_inbox as inbox
from app.services import discussion_picture_cleanup as picture_cleanup
from app.services import discussion_retention as retention
from app.workers import discussion_retention_worker as worker

world = discussion_world
base = base_fixture
targets = targets_fixture
storage = picture_storage
picture_maintenance = picture_maintenance_fixture


@pytest.fixture
def factory(db_connection, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_removed_content_cleanup_enabled", True)
    return sessionmaker(
        bind=db_connection, autoflush=False, join_transaction_mode="create_savepoint"
    )


def remove_message(client, w, made):
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="message.delete",
                thread_id=made["thread_id"],
                message_id=made["message_id"],
                expected_revision=1,
                reason="Sensitive removal reason",
            ),
        ),
    )


def age(db, made, *, thread=False, days=31):
    model, key = (
        (m.DiscussionThread, "thread_id") if thread else (m.DiscussionMessage, "message_id")
    )
    row = db.get(model, uuid.UUID(made[key]))
    row.deleted_at = security.utcnow() - timedelta(days=days)
    db.commit()


def revisions(db, made):
    db.expire_all()
    return list(
        db.scalars(
            sa.select(m.DiscussionMessageRevision).where(
                m.DiscussionMessageRevision.message_id == uuid.UUID(made["message_id"])
            )
        )
    )


def test_purge_erases_bodies_reasons_reactions_and_keeps_identity_receipts(
    client, db_session, world, factory
):
    w = world
    original = create(w, "Private original")
    made = send(client, w, original)
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="reaction.set",
                thread_id=made["thread_id"],
                message_id=made["message_id"],
                emoji="heart",
                present=True,
            ),
        ),
    )
    remove_message(client, w, made)
    age(db_session, made)
    before = {
        model: db_session.scalar(sa.select(sa.func.count()).select_from(model))
        for model in [
            m.DiscussionMessage,
            m.DiscussionMessageRevision,
            m.DiscussionReceipt,
            m.DiscussionEvent,
        ]
    }
    counts = retention.run_batch(factory=factory)
    assert counts.messages == 1 and counts.threads == 0
    assert all(row.body is None and row.reason is None for row in revisions(db_session, made))
    assert (
        db_session.get(m.DiscussionMessage, uuid.UUID(made["message_id"])).content_purged_at
        is not None
    )
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.DiscussionReaction)) == 0
    assert all(
        db_session.scalar(sa.select(sa.func.count()).select_from(model)) == count
        for model, count in before.items()
    )
    send(client, w, original)  # Accepted old receipt cannot republish erased content.
    assert all(row.body is None for row in revisions(db_session, made))
    tombstone = client.get(
        route(w) + f"/threads/{made['thread_id']}/messages/{made['message_id']}",
        headers=headers(w.tokens["owner"]),
    ).json()["message"]
    assert tombstone["deleted"] and tombstone["body"] is None
    assert retention.run_batch(factory=factory).messages == 0


def test_live_recent_and_unknown_removal_clocks_never_purge(client, db_session, world, factory):
    made = [send(client, world, create(world, "Keep body " + str(i))) for i in range(3)]
    remove_message(client, world, made[1])
    remove_message(client, world, made[2])
    db_session.get(m.DiscussionMessage, uuid.UUID(made[2]["message_id"])).deleted_at = None
    db_session.commit()
    assert retention.run_batch(factory=factory).messages == 0
    assert all(revisions(db_session, item)[0].body for item in made)


def test_removed_thread_purges_children_in_bounded_batches_before_anchors(
    client, db_session, world, factory, monkeypatch
):
    w = world
    original = create(w)
    made = send(client, w, original)
    for _ in range(2):
        send(
            client,
            w,
            envelope(
                w,
                dict(
                    type="reply.create",
                    thread_id=made["thread_id"],
                    message_id=str(uuid.uuid4()),
                    body=body("Child body"),
                ),
            ),
        )
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="thread.remove",
                thread_id=made["thread_id"],
                expected_state_revision=1,
                reason="Private thread reason",
            ),
        ),
        actor="owner",
    )
    age(db_session, made, thread=True)
    thread_id = uuid.UUID(made["thread_id"])
    db_session.get(m.DiscussionAnchorRevision, (thread_id, 1)).payload = {
        "retained_quote": "Private target quote"
    }
    db_session.commit()
    monkeypatch.setattr(get_settings(), "collaboration_removed_content_cleanup_batch_size", 1)
    for index in range(3):
        counts = retention.run_batch(factory=factory)
        assert counts.messages == 1 and counts.threads == (1 if index == 2 else 0)
        db_session.expire_all()
        anchor = db_session.get(m.DiscussionAnchorRevision, (thread_id, 1))
        assert anchor.payload == (
            retention.PURGED_ANCHOR if index == 2 else {"retained_quote": "Private target quote"}
        )
    thread = db_session.get(m.DiscussionThread, thread_id)
    assert thread.content_purged_at and thread.removal_reason is None
    assert send(client, w, original) == made  # Metadata-only receipt, no command replay.
    assert all(row.body is None for row in revisions(db_session, made))
    send(client, w, {**original, "operation_id": str(uuid.uuid4())}, expected=409)
    assert (
        client.get(
            route(w) + f"/threads/{thread_id}/state", headers=headers(w.tokens["owner"])
        ).status_code
        == 404
    )
    assert retention.run_batch(factory=factory).messages == 0


def test_purged_bases_anchor_is_unavailable_without_breaking_changes_export_or_inbox(
    client, db_session, targets, factory, monkeypatch
):
    t, w = targets, targets.w
    monkeypatch.setattr(get_settings(), "collaboration_inbox_enabled", True)
    monkeypatch.setattr(get_settings(), "collaboration_export_enabled", True)
    payload = command(t)
    payload["command"]["body"] = mention(w.owner)
    made = send(client, w, payload)
    assert inbox.run_batch() == 1
    assert len(listing(client, w)["items"]) == 1
    events = get(client, t, "/changes", view_id=t.captured["view_id"])
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="thread.remove",
                thread_id=made["thread_id"],
                expected_state_revision=1,
                reason="Synthetic Base removal",
            ),
        ),
        actor="owner",
    )
    age(db_session, made, thread=True)
    assert retention.run_batch(factory=factory).threads == 1
    get(client, t, f"/threads/{made['thread_id']}/state", expected=404)
    send(client, w, payload, expected=404)  # Purged target evidence cannot authorize a receipt.
    delta = get(client, t, "/changes", view_id=t.captured["view_id"], cursor=events["cursor"])
    assert delta["events"] == [] and delta["cursor"] != events["cursor"]
    assert get(client, t, "/threads", view_id=t.captured["view_id"])["threads"] == []
    assert records(archive(client, w, view_id=t.captured["view_id"]), "thread") == []
    assert listing(client, w)["items"] == []


def test_last_retained_picture_reference_expires_atomically_then_sweep_reclaims(
    client, db_session, world, factory, picture_maintenance
):
    w = world
    pid, _ = upload(client, w, raster())
    _, first = publish(client, w, pid)
    second = send(
        client,
        w,
        envelope(
            w,
            dict(
                type="reply.create",
                thread_id=first["thread_id"],
                message_id=str(uuid.uuid4()),
                body=picture_body(pid),
            ),
        ),
    )
    remove_message(client, w, first)
    age(db_session, first)
    first_count = retention.run_batch(factory=factory)
    assert first_count.messages == 1 and first_count.picture_identities_expired == 0
    db_session.expire_all()
    assert db_session.get(m.DiscussionPicture, uuid.UUID(pid)).state == "attached"
    assert picture_maintenance.store.objects
    remove_message(client, w, second)
    age(db_session, second)
    second_count = retention.run_batch(factory=factory)
    assert second_count.messages == 1 and second_count.picture_identities_expired == 1
    db_session.expire_all()
    assert db_session.get(m.DiscussionPicture, uuid.UUID(pid)).state == "expired"
    assert (
        db_session.scalar(sa.select(sa.func.count()).select_from(m.DiscussionPictureReference)) == 0
    )
    assert picture_maintenance.store.objects  # No object I/O happens during SQL purge.
    cleanup = picture_cleanup.run_batch()
    assert cleanup.completed and cleanup.deleted > 0 and not picture_maintenance.store.objects


@pytest.mark.parametrize(
    "cause",
    ["disabled", "stopped", "deadline", "disabled_midway", "stopped_midway", "failure_midway"],
)
def test_stops_and_failures_preserve_uncommitted_content(
    client, db_session, world, factory, monkeypatch, cause
):
    made = send(client, world, create(world))
    remove_message(client, world, made)
    age(db_session, made)
    stop = threading.Event()
    if cause == "disabled":
        monkeypatch.setattr(get_settings(), "collaboration_removed_content_cleanup_enabled", False)
    elif cause == "stopped":
        stop.set()
    elif cause == "deadline":
        monkeypatch.setattr(retention, "BATCH_SECONDS", 0)
    else:
        original = retention.purge_message

        def interrupt(*args):
            result = original(*args)
            if cause == "failure_midway":
                raise RuntimeError("Synthetic transaction interruption")
            if cause == "disabled_midway":
                monkeypatch.setattr(
                    get_settings(), "collaboration_removed_content_cleanup_enabled", False
                )
            else:
                stop.set()
            return result

        monkeypatch.setattr(retention, "purge_message", interrupt)
    if cause == "failure_midway":
        with pytest.raises(RuntimeError, match="Synthetic"):
            retention.run_batch(stop, factory=factory)
    else:
        result = retention.run_batch(stop, factory=factory)
        assert result.stopped and result.messages == 0
    assert revisions(db_session, made)[0].body == body()
    assert (
        db_session.get(m.DiscussionMessage, uuid.UUID(made["message_id"])).content_purged_at is None
    )


def test_config_change_after_candidate_selection_rechecks_cutoff(
    client, db_session, world, factory, monkeypatch
):
    made = send(client, world, create(world))
    remove_message(client, world, made)
    age(db_session, made)
    original = retention.lock_share

    def lengthen(*args):
        row = original(*args)
        monkeypatch.setattr(get_settings(), "collaboration_removed_content_retention_days", 60)
        return row

    monkeypatch.setattr(retention, "lock_share", lengthen)
    assert retention.run_batch(factory=factory).messages == 0
    assert revisions(db_session, made)[0].body == body()


def test_cancelled_retention_worker_drains_its_background_transaction(monkeypatch):
    started, released, finished = threading.Event(), threading.Event(), threading.Event()

    def job(stop):
        started.set()
        assert released.wait(5)
        assert stop.is_set()
        finished.set()
        return retention.Counts(stopped=True)

    monkeypatch.setattr(worker, "run_batch", job)

    async def scenario():
        stop = threading.Event()
        task = asyncio.create_task(worker.finish_batch(stop))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def test_picture_reference_retention_index_migrates_without_data_loss():
    module = importlib.import_module(
        "app.db.migrations.versions.202611070004_discussion_retention_index"
    )
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as db:
            db.execute(
                sa.text(
                    "CREATE TABLE discussion_picture_references "
                    "(picture_id TEXT, message_id TEXT, revision INTEGER)"
                )
            )
            db.execute(sa.text("INSERT INTO discussion_picture_references VALUES ('p','m',1)"))
            with Operations.context(MigrationContext.configure(db)):
                module.upgrade()
                assert sa.inspect(db).get_indexes("discussion_picture_references")[0][
                    "column_names"
                ] == ["message_id", "revision"]
                module.downgrade()
                assert db.scalar(sa.text("SELECT count(*) FROM discussion_picture_references")) == 1
                module.upgrade()
    finally:
        engine.dispose()

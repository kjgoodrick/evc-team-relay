import json
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from test_base_discussions import base as base_fixture
from test_base_discussions import command, deny_record
from test_base_discussions import targets as targets_fixture
from test_discussion_pictures import picture_body, raster, upload
from test_discussion_pictures import storage as picture_storage
from test_discussions import base as route
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_export as export

world = discussion_world
base = base_fixture
targets = targets_fixture
storage = picture_storage


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_export_enabled", True)


def archive(client, w, actor="reader", expected=200, **params):
    response = client.get(route(w) + "/export", headers=headers(w.tokens[actor]), params=params)
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "application/json"
    value = response.json()
    if expected != 200:
        assert set(value) == {"error"}
        assert set(value["error"]) == {"code", "message", "request_id"}
        return value["error"]["message"]
    return value


def records(value, kind):
    return [row for row in value["records"] if row["type"] == kind]


def test_reader_snapshot_keeps_history_reactions_and_identity_without_writes(
    client, db_session, world
):
    w = world
    made = send(client, w, create(w, "Original export body"))
    tid, mid = made["thread_id"], made["message_id"]
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": tid,
                "message_id": mid,
                "expected_revision": 1,
                "body": body("Edited export body"),
            },
        ),
    )
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "reaction.set",
                "thread_id": tid,
                "message_id": mid,
                "emoji": "thumbs_up",
                "present": True,
            },
        ),
    )
    before = {
        model: len(list(db_session.scalars(select(model))))
        for model in [
            m.DiscussionReceipt,
            m.DiscussionEvent,
            m.DiscussionScope,
            m.DiscussionMessageRevision,
        ]
    }
    value = archive(client, w)
    assert value["format"] == "team-relay-discussion-export" and value["version"] == 1
    assert value["entry"]["id"] == w.entry.id and value["entry"]["path"] == w.entry.path
    assert records(value, "thread")[0]["id"] == tid
    assert records(value, "message")[0]["revision"] == 2
    assert [r["body"] for r in records(value, "message_revision")] == [
        body("Original export body"),
        body("Edited export body"),
    ]
    assert records(value, "reaction")[0] == {
        "type": "reaction",
        "message_id": mid,
        "emoji": "thumbs_up",
        "count": 1,
        "mine": False,
    }
    assert records(value, "identity") == [{"type": "identity", "id": str(w.reviewer.id)}]
    assert "@discussion.test" not in json.dumps(value)
    assert all(
        len(list(db_session.scalars(select(model)))) == count for model, count in before.items()
    )
    assert archive(client, w)["records"] == value["records"]


@pytest.mark.parametrize("actor", ["reader", "reviewer", "owner"])
def test_removed_bodies_and_thread_anchors_are_never_exported(client, world, actor):
    w = world
    removed = send(client, w, create(w, "Do not export removed message"))
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.delete",
                "thread_id": removed["thread_id"],
                "message_id": removed["message_id"],
                "expected_revision": 1,
            },
        ),
    )
    other = send(client, w, create(w, "Do not export removed thread"))
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.remove",
                "thread_id": other["thread_id"],
                "expected_state_revision": 1,
                "reason": "Synthetic moderation",
            },
        ),
        actor="owner",
    )
    value = archive(client, w, actor)
    assert [row["id"] for row in records(value, "thread")] == [removed["thread_id"]]
    assert records(value, "message")[0]["deleted"] is True
    assert records(value, "message_revision") == []
    assert "Do not export" not in json.dumps(value)
    assert other["thread_id"] not in json.dumps(value)


def test_empty_scope_does_not_create_rows_and_disabled_or_denied_is_bounded(
    client, db_session, world, monkeypatch
):
    w = world
    assert archive(client, w)["records"] == []
    assert db_session.scalar(select(m.DiscussionScope)) is None
    monkeypatch.setattr(get_settings(), "collaboration_export_enabled", False)
    assert archive(client, w, expected=404)["code"] == "discussion_export_disabled"
    monkeypatch.setattr(get_settings(), "collaboration_export_enabled", True)
    response = client.get(route(w, w.other.id) + "/export", headers=headers(w.tokens["reader"]))
    assert response.status_code == 404
    archive(client, w, expected=422, view_id=str(uuid.uuid4()))


def test_saved_view_exports_exclude_other_views_and_inaccessible_record_targets(
    client, db_session, targets
):
    t, w = targets, targets.w
    first = send(client, w, command(t))
    second = send(client, w, command(t, second=True))
    view = t.captured["view_id"]
    assert [
        row["id"] for row in records(archive(client, w, "reviewer", view_id=view), "thread")
    ] == [first["thread_id"]]
    assert second["thread_id"] not in json.dumps(archive(client, w, "reviewer", view_id=view))
    deny_record(db_session, t)
    assert archive(client, w, "reviewer", view_id=view)["records"] == []
    archive(client, w, "reviewer", expected=422)
    assert (
        records(archive(client, w, "owner", view_id=view), "thread")[0]["id"] == first["thread_id"]
    )


def test_picture_metadata_only_for_exported_live_history_and_no_object_io(client, world, storage):
    w = world
    picture_id = upload(client, w, raster())[0]
    payload = create(w)
    payload["command"]["body"] = picture_body(picture_id)
    made = send(client, w, payload)
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": made["thread_id"],
                "message_id": made["message_id"],
                "expected_revision": 1,
                "body": body("Picture retained in live history"),
            },
        ),
    )
    gets = storage.gets
    value = archive(client, w)
    picture = records(value, "picture")[0]
    assert picture["available"] and picture["picture_id"] == picture_id
    assert "key" not in json.dumps(picture["metadata"])
    assert storage.gets == gets
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.delete",
                "thread_id": made["thread_id"],
                "message_id": made["message_id"],
                "expected_revision": 2,
            },
        ),
    )
    assert records(archive(client, w, "owner"), "picture") == []


@pytest.mark.parametrize(
    "setting,value",
    [("MAX_RECORDS", 1), ("MAX_BYTES", 100), ("MAX_SCANNED", 1), ("MAX_SECONDS", -1)],
)
def test_limits_return_no_partial_archive_and_release_capacity(
    client, world, monkeypatch, setting, value
):
    send(client, world, create(world, "Never in an error response"))
    before = getattr(export, setting)
    monkeypatch.setattr(export, setting, value)
    result = archive(client, world, expected=413)
    assert result == {"code": "discussion_export_limit"}
    monkeypatch.setattr(export, setting, before)
    assert records(archive(client, world), "thread")


def test_capacity_and_storage_errors_do_not_leak_bodies_or_leave_a_slot(client, world, monkeypatch):
    assert export.slots.acquire(blocking=False) and export.slots.acquire(blocking=False)
    try:
        assert archive(client, world, expected=503)["code"] == "discussion_export_busy"
    finally:
        export.slots.release()
        export.slots.release()
    send(client, world, create(world))
    original = export._thread

    def failure(*args):
        raise OperationalError("Private SQL body", {}, RuntimeError("private details"))

    monkeypatch.setattr(export, "_thread", failure)
    assert archive(client, world, expected=503) == {"code": "retryable_transaction"}
    monkeypatch.setattr(export, "_thread", original)
    assert records(archive(client, world), "thread")


def test_query_count_does_not_grow_per_message(client, db_session, world):
    made = send(client, world, create(world))

    def measured():
        queries = []

        def count(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", count)
        try:
            value = archive(client, world)
            return value, len(queries)
        finally:
            event.remove(engine, "before_cursor_execute", count)

    _, initial = measured()
    for n in range(20):
        send(
            client,
            world,
            envelope(
                world,
                {
                    "type": "reply.create",
                    "thread_id": made["thread_id"],
                    "message_id": str(uuid.uuid4()),
                    "body": body(str(n)),
                },
            ),
        )
    value, final = measured()
    assert len(records(value, "message")) == 21
    assert final == initial


def test_unavailable_historical_base_anchor_is_redacted_even_when_current_view_is_readable(
    client, db_session, targets
):
    t, w = targets, targets.w
    made = send(client, w, command(t, "view"))
    tid = uuid.UUID(made["thread_id"])
    prior = db_session.get(m.DiscussionAnchorRevision, (tid, 1))
    current = dict(prior.payload)
    # A retained legacy target may have dependencies no longer present in the
    # current target. Export must authorize each history record independently.
    prior.payload = {**command(t)["command"]["anchor"], "entry_id": w.entry.id}
    db_session.add(
        m.DiscussionAnchorRevision(thread_id=tid, revision=2, payload=current, actor_id=w.owner.id)
    )
    db_session.get(m.DiscussionThread, tid).anchor_revision = 2
    db_session.commit()
    deny_record(db_session, t)
    value = archive(client, w, "reviewer", view_id=t.captured["view_id"])
    history = records(value, "anchor_revision")
    assert len(history) == 2
    assert history[0]["anchor"] is None and history[0]["unavailable"]
    assert history[1]["anchor"]["target"]["type"] == "view" and not history[1]["unavailable"]
    assert str(t.record.id) not in json.dumps(value)


def test_export_capability_matches_its_independent_default_off_gate(client, world, monkeypatch):
    for enabled in [False, True]:
        monkeypatch.setattr(get_settings(), "collaboration_export_enabled", enabled)
        response = client.get(
            "/v1/collaboration/capabilities",
            params={"share_id": str(world.share.id)},
            headers=headers(world.tokens["reader"]),
        )
        assert response.status_code == 200
        assert response.json()["features"]["discussion_export"] is enabled

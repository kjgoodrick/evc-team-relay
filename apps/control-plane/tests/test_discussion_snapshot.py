import json
import uuid

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import OperationalError
from test_base_discussions import base as base_fixture
from test_base_discussions import command, deny_record
from test_base_discussions import targets as targets_fixture
from test_discussions import base as route
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_snapshot as snapshot

world = discussion_world
base = base_fixture
targets = targets_fixture


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_snapshot_enabled", True)


def read(client, w, actor="reader", expected=200, **params):
    response = client.get(route(w) + "/snapshot", headers=headers(w.tokens[actor]), params=params)
    assert response.status_code == expected, response.text
    value = response.json()
    if expected != 200:
        return value["error"]["message"]
    assert response.headers["cache-control"] == "no-store"
    assert value["protocol_version"] == 1 and value["entry_id"] == w.entry.id
    return value


def test_current_state_and_cursor_form_a_baseline_without_historical_bodies(client, world):
    w = world
    made = send(client, w, create(w, "Old historical body"))
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
                "body": body("Current body"),
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
    before = read(client, w, "reviewer")
    assert len(before["threads"]) == 1
    thread = before["threads"][0]
    assert thread["thread"]["id"] == tid
    assert thread["anchor"] == {"version": 1, "type": "note"}
    assert thread["messages"][0]["body"] == body("Current body")
    assert thread["messages"][0]["reactions"] == [{"emoji": "thumbs_up", "count": 1, "mine": True}]
    assert "Old historical body" not in json.dumps(before)
    assert thread["thread"]["activity_sequence"] == before["through_sequence"]
    new = send(client, w, create(w, "After snapshot"))
    response = client.get(
        route(w) + "/changes",
        params={"cursor": before["cursor"]},
        headers=headers(w.tokens["reviewer"]),
    )
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert [item["data"]["thread_id"] for item in events] == [new["thread_id"]]
    assert all(item["sequence"] > before["through_sequence"] for item in events)


@pytest.mark.parametrize("actor", ["reader", "reviewer", "owner"])
def test_removed_content_is_absent_for_every_role(client, world, actor):
    w = world
    removed = send(client, w, create(w, "Removed message secret"))
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
    other = send(client, w, create(w, "Removed thread secret"))
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
    value = read(client, w, actor)
    assert [row["thread"]["id"] for row in value["threads"]] == [removed["thread_id"]]
    assert value["threads"][0]["messages"][0]["deleted"] is True
    assert value["threads"][0]["messages"][0]["body"] is None
    assert "secret" not in json.dumps(value) and other["thread_id"] not in json.dumps(value)


def test_empty_read_is_read_only_and_capability_follows_its_gate(
    client, db_session, world, monkeypatch
):
    w = world
    assert read(client, w)["threads"] == []
    assert db_session.scalar(select(m.DiscussionScope)) is None
    assert db_session.scalar(select(m.DiscussionEvent)) is None
    value = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(w.share.id)},
        headers=headers(w.tokens["reader"]),
    ).json()
    assert value["features"]["discussion_snapshot"] is True
    monkeypatch.setattr(get_settings(), "collaboration_snapshot_enabled", False)
    assert read(client, w, expected=404) == {"code": "discussion_snapshot_disabled"}
    value = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(w.share.id)},
        headers=headers(w.tokens["reader"]),
    ).json()
    assert value["features"]["discussion_snapshot"] is False


def test_saved_view_and_dependency_permissions_are_checked(client, db_session, targets):
    t, w = targets, targets.w
    first = send(client, w, command(t))
    other = send(client, w, command(t, second=True))
    view = t.captured["view_id"]
    value = read(client, w, "reviewer", view_id=view)
    assert [row["thread"]["id"] for row in value["threads"]] == [first["thread_id"]]
    assert other["thread_id"] not in json.dumps(value)
    deny_record(db_session, t)
    assert read(client, w, "reviewer", view_id=view)["threads"] == []
    read(client, w, "reviewer", expected=422)
    assert (
        read(client, w, "owner", view_id=view)["threads"][0]["thread"]["id"] == first["thread_id"]
    )


@pytest.mark.parametrize(
    "setting,value",
    [("MAX_RECORDS", 1), ("MAX_BYTES", 32), ("MAX_SCANNED", 0), ("MAX_SECONDS", -1)],
)
def test_limits_return_no_partial_snapshot_and_release_capacity(
    client, world, monkeypatch, setting, value
):
    send(client, world, create(world, "Never in an error response"))
    before = getattr(snapshot, setting)
    monkeypatch.setattr(snapshot, setting, value)
    assert read(client, world, expected=413) == {"code": "discussion_snapshot_limit"}
    monkeypatch.setattr(snapshot, setting, before)
    assert len(read(client, world)["threads"]) == 1


def test_capacity_and_database_failures_are_bounded_and_retryable(client, world, monkeypatch):
    assert snapshot.slots.acquire(blocking=False) and snapshot.slots.acquire(blocking=False)
    try:
        assert read(client, world, expected=503) == {"code": "discussion_snapshot_busy"}
    finally:
        snapshot.slots.release()
        snapshot.slots.release()
    original = snapshot._snapshot

    def fail(*args):
        raise OperationalError("Private SQL", {}, RuntimeError("private details"))

    monkeypatch.setattr(snapshot, "_snapshot", fail)
    assert read(client, world, expected=503) == {"code": "retryable_transaction"}
    monkeypatch.setattr(snapshot, "_snapshot", original)
    assert read(client, world)["threads"] == []


@pytest.mark.parametrize("missing", ["anchor", "body"])
def test_missing_current_revision_is_not_silently_omitted(client, db_session, world, missing):
    made = send(client, world, create(world))
    if missing == "anchor":
        db_session.execute(
            delete(m.DiscussionAnchorRevision).where(
                m.DiscussionAnchorRevision.thread_id == uuid.UUID(made["thread_id"])
            )
        )
    else:
        db_session.execute(
            delete(m.DiscussionMessageRevision).where(
                m.DiscussionMessageRevision.message_id == uuid.UUID(made["message_id"])
            )
        )
    db_session.commit()
    assert read(client, world, expected=500) == {"code": "invalid_stored_" + missing}


@pytest.mark.parametrize("invalid", [None, {"private_invalid_body": "must not appear"}])
def test_current_body_corruption_returns_no_partial_body(client, db_session, world, invalid):
    made = send(client, world, create(world))
    row = db_session.get(m.DiscussionMessageRevision, (uuid.UUID(made["message_id"]), 1))
    row.body = invalid
    db_session.commit()
    assert read(client, world, expected=500) == {"code": "invalid_stored_body"}


def test_empty_current_thread_is_not_a_complete_baseline(client, db_session, world):
    made = send(client, world, create(world))
    message_id = uuid.UUID(made["message_id"])
    db_session.execute(
        delete(m.DiscussionMessageRevision).where(
            m.DiscussionMessageRevision.message_id == message_id
        )
    )
    db_session.execute(delete(m.DiscussionMessage).where(m.DiscussionMessage.id == message_id))
    db_session.commit()
    assert read(client, world, expected=500) == {"code": "invalid_stored_body"}


def test_revoked_membership_refuses_the_entire_read(client, db_session, world):
    send(client, world, create(world))
    db_session.execute(
        delete(m.ShareMember).where(
            m.ShareMember.share_id == world.share.id, m.ShareMember.user_id == world.reader.id
        )
    )
    db_session.commit()
    assert read(client, world, expected=404) == {"code": "target_unavailable"}


def test_snapshot_explicitly_identifies_its_restore_generation(client, db_session, world):
    assert read(client, world)["restore_generation"] is None
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()
    assert read(client, world)["restore_generation"] == str(generation)


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
            value = read(client, world)
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
    assert len(value["threads"][0]["messages"]) == 21
    assert final == initial

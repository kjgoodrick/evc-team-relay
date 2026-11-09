from types import SimpleNamespace

import pytest
from test_base_discussions import anchor, context, deny_record, get
from test_discussions import create, headers, send
from test_discussions import world as discussion_world
from test_inline_bases_registry import inline as inline_fixture
from test_inline_bases_registry import request

from app.core.config import get_settings

world = discussion_world
inline = inline_fixture


@pytest.fixture
def targets(client, world, inline, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_discussions_enabled", True)
    state, generation = inline
    snapshot = request(client, world, generation).json()
    captured = context(client, world, snapshot["views"][0]["id"], record=world.entry)
    return SimpleNamespace(w=world, record=world.entry, snapshot=snapshot, captured=captured)


@pytest.mark.parametrize("kind", ["view", "item", "cell", "property"])
def test_inline_views_accept_comments_and_keep_discussions_separate(client, targets, kind):
    t = targets
    payload = create(t.w)
    payload["command"]["anchor"] = anchor(t, kind)
    first = send(client, t.w, payload)
    second_payload = create(t.w)
    second_payload["command"]["anchor"] = anchor(t, kind, second=True)
    second = send(client, t.w, second_payload)
    page = get(client, t, "/threads", view_id=t.captured["view_id"])
    assert [thread["id"] for thread in page["threads"]] == [first["thread_id"]]
    other = get(client, t, "/threads", view_id=t.snapshot["views"][1]["id"])
    assert [thread["id"] for thread in other["threads"]] == [second["thread_id"]]
    assert get(client, t, f"/threads/{first['thread_id']}")["anchor"]["type"] == "base"
    send(client, t.w, payload, actor="reader", expected=403)


def test_inline_record_permissions_apply_to_note_feed_direct_reads_and_receipts(
    client, targets, db_session
):
    import uuid

    from app.db import models as m

    t = targets
    record = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=t.w.share.id,
        path="Notes/Record.md",
        record={"type": "markdown"},
        deleted=False,
    )
    db_session.add(record)
    db_session.commit()
    t.record = record
    t.captured = context(client, t.w, t.captured["view_id"], record=record)
    payload = create(t.w)
    payload["command"]["anchor"] = anchor(t, "cell")
    posted = send(client, t.w, payload)
    deny_record(db_session, t)
    get(client, t, f"/threads/{posted['thread_id']}", expected=404)
    assert get(client, t, "/threads")["threads"] == []
    assert get(client, t, "/changes")["events"] == []
    send(client, t.w, payload, expected=404)


def test_inline_view_id_cannot_be_used_from_another_note(client, targets, db_session):
    import uuid

    from app.db import models as m

    t = targets
    note = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=t.w.share.id,
        path="Notes/Other.md",
        record={"type": "markdown"},
        collaboration_generation=t.w.entry.collaboration_generation,
        deleted=False,
    )
    db_session.add(note)
    db_session.commit()
    path = f"/v1/collaboration/shares/{t.w.share.id}/entries/{note.id}/bases/target-context"
    response = client.post(
        path, json={"view_id": t.captured["view_id"]}, headers=headers(t.w.tokens["reviewer"])
    )
    assert response.status_code == 404


def test_inline_formula_and_group_catalog_keeps_view_scope(client, targets, inline):
    from test_base_target_definitions import catalog

    t = targets
    state, generation = inline
    state["formulas"] = {"double": "price * 2"}
    state["views"][0]["groupBy"] = {"property": "status", "direction": "ASC"}
    snapshot = request(client, t.w, generation).json()
    page = catalog(client, t)
    assert page["registry_revision"] == snapshot["revision"]
    assert {(item["kind"], item["property"]["name"]) for item in page["targets"]} == {
        ("formula", "double"),
        ("group", "status"),
    }
    other = catalog(client, t, view_id=t.snapshot["views"][1]["id"])
    assert all(item["kind"] != "group" for item in other["targets"])
    assert page["computed_values"] is False


def test_inline_timeline_markers_create_read_comment_and_archive(
    client, targets, inline, monkeypatch
):
    from test_timeline_markers import archive, catalog, marker_path, mutate, put, thread

    t = targets
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_ranges_enabled", True)
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_markers_enabled", True)
    state, generation = inline
    state["views"][0].update(
        type="sabidurian-timeline", startDateProp="note.start", timeZone="America/Denver"
    )
    t.snapshot = request(client, t.w, generation).json()
    t.captured = context(client, t.w, t.snapshot["views"][0]["id"])
    payload = put(client, t)
    mutate(client, t, payload, actor="reviewer", expected=403)
    created = mutate(client, t, payload)
    assert mutate(client, t, payload) == created
    status = client.post(
        marker_path(t) + "/commands/status", json=payload, headers=headers(t.w.tokens["owner"])
    )
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "accepted"
    page = catalog(client, t)
    assert len(page["markers"]) == 1
    marker = created["marker"]
    response = client.get(
        marker_path(t) + "/" + marker["id"], headers=headers(t.w.tokens["reviewer"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["marker"]["id"] == marker["id"]
    posted = send(client, t.w, thread(client, t))
    assert (
        get(client, t, f"/threads/{posted['thread_id']}")["anchor"]["target"]["type"]
        == "timeline_marker"
    )
    other = request(client, t.w, generation, start="Aw==").json()
    assert catalog(client, t, view_id=other["views"][0]["id"])["markers"] == []
    mutate(client, t, archive(client, t, marker))
    assert catalog(client, t)["markers"] == []


def test_inline_export_catalog_and_archives_keep_note_and_views_separate(
    client, targets, monkeypatch
):
    from test_discussion_export import archive, catalog, records

    monkeypatch.setattr(get_settings(), "collaboration_export_enabled", True)
    t = targets
    note = send(client, t.w, create(t.w, "Ordinary note discussion"))
    first = create(t.w, "First inline view")
    first["command"]["anchor"] = anchor(t, "view")
    posted = send(client, t.w, first)
    second = create(t.w, "Second inline view")
    second["command"]["anchor"] = anchor(t, "view", second=True)
    other = send(client, t.w, second)
    scopes = catalog(client, t.w, "reviewer")["scopes"]
    assert len(scopes) == 3
    assert {(item["kind"], item["view_id"]) for item in scopes} == {
        ("note", None),
        ("base", t.captured["view_id"]),
        ("base", t.snapshot["views"][1]["id"]),
    }
    ordinary = archive(client, t.w, "reviewer")
    assert [item["id"] for item in records(ordinary, "thread")] == [note["thread_id"]]
    for view_id, expected in (
        (t.captured["view_id"], posted),
        (t.snapshot["views"][1]["id"], other),
    ):
        exported = archive(client, t.w, "reviewer", view_id=view_id)
        assert exported["entry"]["kind"] == "base"
        assert exported["entry"]["path"] == t.w.entry.path
        assert exported["view_id"] == view_id
        assert [item["id"] for item in records(exported, "thread")] == [expected["thread_id"]]

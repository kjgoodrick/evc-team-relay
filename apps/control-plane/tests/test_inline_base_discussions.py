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

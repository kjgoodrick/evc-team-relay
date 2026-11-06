import uuid

import pytest
from test_base_definition_discussions import base, command, definitions, targets, world
from test_base_discussions import context, deny_record, get
from test_bases_registry import inspect, view
from test_discussions import body, envelope, headers, send

from app.core.config import get_settings
from app.db import models as m
from app.services.base_discussions import RECORD_TYPES

# Make the imported dependency fixtures explicit to pytest and static analysis.
world, base, targets, definitions = world, base, targets, definitions


@pytest.fixture
def cells(definitions, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_formula_cells_enabled", True)
    return definitions


def cell_command(client, t):
    payload = command(client, t)
    target = payload["command"]["anchor"]["target"]
    captured = context(client, t.w, t.captured["view_id"], record=t.record)
    target.update(
        type="formula_cell",
        record_id=captured["record_id"],
        record_generation=captured["record_generation"],
    )
    return payload


@pytest.mark.parametrize("targets", RECORD_TYPES, indirect=True)
def test_formula_cells_bind_record_and_definition_without_evaluating_or_copying_values(
    client, db_session, cells
):
    t = cells
    before = t.record.record.copy()
    original = cell_command(client, t)
    accepted = send(client, t.w, original)
    assert send(client, t.w, original) == accepted
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == original["command"]["anchor"]["target"]
    )
    assert db_session.get(m.ManagedEntry, t.record.id).record == before
    for field in ("computed_value", "dependency_ids", "quote"):
        malformed = cell_command(client, t)
        malformed["command"]["anchor"]["target"][field] = "unverified"
        send(client, t.w, malformed, expected=422)
    deny_record(db_session, t, read=False)
    send(client, t.w, original, expected=403)
    send(client, t.w, cell_command(client, t), expected=403)
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="reply.create",
                thread_id=accepted["thread_id"],
                message_id=str(uuid.uuid4()),
                body=body(),
            ),
        ),
        expected=403,
    )
    get(client, t, f"/threads/{accepted['thread_id']}")


@pytest.mark.parametrize("change", ["read", "delete", "generation"])
def test_record_loss_hides_formula_cell_history_receipts_events_and_replies(
    client, db_session, cells, change
):
    t = cells
    original = cell_command(client, t)
    accepted = send(client, t.w, original)
    if change == "read":
        deny_record(db_session, t)
    else:
        if change == "delete":
            t.record.deleted = True
        else:
            t.record.collaboration_generation = uuid.uuid4()
        db_session.commit()
    thread, message = accepted["thread_id"], accepted["message_id"]
    for suffix in (
        f"/threads/{thread}",
        f"/threads/{thread}/state",
        f"/threads/{thread}/messages/{message}/history",
    ):
        get(client, t, suffix, expected=404)
    send(client, t.w, original, expected=404)
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(type="reply.create", thread_id=thread, message_id=str(uuid.uuid4()), body=body()),
        ),
        expected=404,
    )
    assert get(client, t, "/threads", view_id=t.captured["view_id"])["threads"] == []
    assert get(client, t, "/changes", view_id=t.captured["view_id"])["events"] == []


def test_formula_edits_preserve_history_but_require_fresh_cell_capture(client, cells):
    t = cells
    original, stale = cell_command(client, t), cell_command(client, t)
    accepted = send(client, t.w, original)
    t.base.save(
        [view("Renamed", summaries={"formula.total": "Average"}), view("Cards", "cards")],
        formulas={"total": "price * 2"},
    )
    inspect(client, t.w)
    send(client, t.w, stale)
    stale = cell_command(client, t)
    t.base.save([view("Renamed"), view("Cards", "cards")], formulas={"total": "price * 3"})
    inspect(client, t.w)
    assert "base_target_definition_changed" in str(send(client, t.w, stale, expected=409))
    assert send(client, t.w, original) == accepted
    send(client, t.w, cell_command(client, t))
    t.base.save([view("Renamed"), view("Cards", "cards")])
    inspect(client, t.w)
    get(client, t, f"/threads/{accepted['thread_id']}")
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="reply.create",
                thread_id=accepted["thread_id"],
                message_id=str(uuid.uuid4()),
                body=body(),
            ),
        ),
    )


def test_formula_cell_flag_and_mentions_keep_record_access_independent(
    client, db_session, cells, monkeypatch
):
    t = cells
    original = cell_command(client, t)
    accepted = send(client, t.w, original)
    deny_record(db_session, t, actor="reader")
    mentioned = cell_command(client, t)
    mentioned["command"]["body"]["blocks"][0]["children"].append(
        {"type": "mention", "user_id": str(t.w.reader.id)}
    )
    send(client, t.w, mentioned, expected=422)
    monkeypatch.setattr(get_settings(), "collaboration_bases_formula_cells_enabled", False)
    caps = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reviewer"]),
    ).json()
    assert "formula" in caps["discussion_base_targets"]
    assert "formula_cell" not in caps["discussion_base_targets"]
    assert "base_formula_cells_disabled" in str(
        send(client, t.w, cell_command(client, t), expected=409)
    )
    assert send(client, t.w, original) == accepted
    get(client, t, f"/threads/{accepted['thread_id']}")


def test_formula_cell_cannot_substitute_foreign_record_lineage_view_or_definition(client, cells):
    t = cells
    for patch in (
        {"record_id": t.w.other.id},
        {"record_generation": str(uuid.uuid4())},
        {"definition_id": str(uuid.uuid4())},
        {"definition_revision": 9007199254740991},
    ):
        payload = cell_command(client, t)
        payload["command"]["anchor"]["target"].update(patch)
        send(client, t.w, payload, expected=404 if "record_id" in patch else 409)
    payload = cell_command(client, t)
    payload["command"]["anchor"]["view_id"] = t.snapshot["views"][1]["id"]
    send(client, t.w, payload, expected=409)

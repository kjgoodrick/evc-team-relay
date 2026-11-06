import uuid

import pytest
from test_base_discussions import anchor, get
from test_base_discussions import targets as target_fixture
from test_base_target_definitions import catalog
from test_bases_registry import base as base_fixture
from test_bases_registry import inspect, view
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m

world = discussion_world
base = base_fixture
targets = target_fixture


@pytest.fixture
def definitions(client, targets, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_definition_targets_enabled", True)
    t = targets
    t.base.save(
        [view("Table", summaries={"formula.total": "Average"}), view("Cards", "cards")],
        formulas={"total": "price * 2"},
    )
    inspect(client, t.w)
    return t


def command(client, t, kind="formula"):
    page = catalog(client, t)
    definition = next(item for item in page["targets"] if item["kind"] == kind)
    result = create(t.w)
    result["command"]["anchor"] = {
        **anchor(t, "view"),
        "registry_revision": page["registry_revision"],
        "target": {
            "type": kind,
            "definition_id": definition["id"],
            "definition_revision": definition["revision"],
            "property": definition["property"],
            **({"summary": definition["summary"]} if kind == "summary" else {}),
        },
    }
    return result


@pytest.mark.parametrize("kind", ["formula", "summary"])
def test_definition_threads_use_view_authority_and_never_accept_computed_values(
    client, db_session, definitions, kind
):
    t = definitions
    payload = command(client, t, kind)
    accepted = send(client, t.w, payload)
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == payload["command"]["anchor"]["target"]
    )
    assert [
        thread["id"]
        for thread in get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]
    ] == [accepted["thread_id"]]
    assert get(client, t, "/threads", view_id=t.snapshot["views"][1]["id"])["threads"] == []
    send(client, t.w, command(client, t, kind), actor="reader", expected=403)
    for field in ("computed_value", "record_id", "dependency_ids"):
        malformed = command(client, t, kind)
        malformed["command"]["anchor"]["target"][field] = "not authorized by a definition"
        send(client, t.w, malformed, expected=422)
    # A definition discussion cannot acquire authority over row data. It is
    # still readable after an unrelated record becomes inaccessible.
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reviewer.id,
            path=t.record.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    get(client, t, f"/threads/{accepted['thread_id']}")


@pytest.mark.parametrize("kind", ["formula", "summary"])
def test_changed_deleted_and_restored_definitions_keep_history_but_reject_stale_creation(
    client, definitions, kind
):
    t = definitions
    original = command(client, t, kind)
    accepted = send(client, t.w, original)
    stale = command(client, t, kind)
    t.base.save(
        [view("Table", summaries={"formula.total": "Average"}), view("Cards", "cards")],
        formulas={"total": "price * 3"},
    )
    inspect(client, t.w)
    rejected = send(client, t.w, stale, expected=409)
    assert "base_target_definition_changed" in str(rejected)
    assert send(client, t.w, original) == accepted
    fresh = send(client, t.w, command(client, t, kind))
    assert fresh["thread_id"] != accepted["thread_id"]
    t.base.save([view("Table"), view("Cards", "cards")])
    inspect(client, t.w)
    get(client, t, f"/threads/{accepted['thread_id']}")
    reply = envelope(
        t.w,
        {
            "type": "reply.create",
            "thread_id": accepted["thread_id"],
            "message_id": str(uuid.uuid4()),
            "body": body("Original discussion"),
        },
    )
    send(client, t.w, reply)
    t.base.save(
        [view("Table", summaries={"formula.total": "Average"}), view("Cards", "cards")],
        formulas={"total": "price * 2"},
    )
    inspect(client, t.w)
    old_again = create(t.w)
    old_again["command"]["anchor"] = stale["command"]["anchor"]
    send(client, t.w, old_again, expected=409)
    get(client, t, f"/threads/{accepted['thread_id']}")


def test_definition_target_cannot_forge_another_view_kind_name_or_future_revision(
    client, definitions
):
    t = definitions
    mutations = [
        lambda a: a.update(view_id=t.snapshot["views"][1]["id"]),
        lambda a: a["target"].update(definition_id=str(uuid.uuid4())),
        lambda a: a["target"].update(definition_revision=a["target"]["definition_revision"] + 1),
    ]
    for mutate in mutations:
        payload = command(client, t)
        mutate(payload["command"]["anchor"])
        send(client, t.w, payload, expected=409)
    for property_value in (
        {"source": "formula", "name": "another"},
        {"source": "note", "name": "total"},
    ):
        payload = command(client, t)
        payload["command"]["anchor"]["target"]["property"] = property_value
        send(client, t.w, payload, expected=422)
    summary = command(client, t, "summary")
    summary["command"]["anchor"]["target"]["summary"] = "Sum"
    send(client, t.w, summary, expected=422)


def test_disable_creation_preserves_existing_threads_and_current_base_revocation_still_hides_them(
    client, db_session, definitions, monkeypatch
):
    t = definitions
    accepted = send(client, t.w, command(client, t))
    monkeypatch.setattr(get_settings(), "collaboration_bases_definition_targets_enabled", False)
    caps = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reviewer"]),
    ).json()
    assert caps["discussion_base_targets"] == ["view", "item", "cell", "property"]
    send(client, t.w, command(client, t), expected=409)
    get(client, t, f"/threads/{accepted['thread_id']}")
    send(
        client,
        t.w,
        envelope(
            t.w,
            {
                "type": "reply.create",
                "thread_id": accepted["thread_id"],
                "message_id": str(uuid.uuid4()),
                "body": body("Still available"),
            },
        ),
    )
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reviewer.id,
            path=t.w.entry.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)

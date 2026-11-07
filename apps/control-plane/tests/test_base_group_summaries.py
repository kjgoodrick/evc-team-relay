"""Group summary definitions keep their exact group witness, never aggregate authority."""

import copy
import uuid

import pytest
from test_base_discussions import deny_record, get
from test_base_groups import (
    add_record,
    base,
    capture,
    groups,
    request,
    storage,
    targets,
    thread,
    world,
)
from test_base_target_definitions import catalog
from test_bases_registry import inspect, view
from test_discussion_pictures import get_picture, picture_body, raster, upload
from test_discussions import body, envelope, headers, send

from app.core.config import get_settings

world, base, targets, groups, storage = world, base, targets, groups, storage


def save(t, *, summary="Average", grouping="status", formula="score * 2"):
    t.base.save(
        [
            view("Table", groupBy={"property": grouping}, summaries={"formula.total": summary}),
            view("Cards", "cards"),
        ],
        formulas={"total": formula},
    )


@pytest.fixture
def summaries(client, groups, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_group_summaries_enabled", True)
    save(groups)
    inspect(client, groups.w)
    return groups


def command(client, t, *, record=None, value=None):
    captured = capture(client, t, request(client, t, record=record, value=value))
    page = catalog(client, t)
    definition = next(item for item in page["targets"] if item["kind"] == "summary")
    payload = thread(t, captured)
    payload["command"]["anchor"]["target"] = {
        "type": "group_summary",
        "definition_id": definition["id"],
        "definition_revision": definition["revision"],
        "property": definition["property"],
        "summary": definition["summary"],
        "group": captured["anchor"]["target"],
    }
    return payload


def test_group_summary_keeps_both_definitions_and_does_not_require_record_comment_permission(
    client, db_session, summaries
):
    t = summaries
    deny_record(db_session, t, read=False)
    original = command(client, t)
    accepted = send(client, t.w, original)
    assert send(client, t.w, original) == accepted
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == original["command"]["anchor"]["target"]
    )
    for field in ("computed_value", "dependency_ids", "quote", "record_id"):
        malformed = copy.deepcopy(original)
        malformed["command"]["anchor"]["target"][field] = "unverified"
        send(client, t.w, malformed, expected=422)
    send(client, t.w, command(client, t), actor="reader", expected=403)


@pytest.mark.parametrize("loss", ["read", "delete", "generation"])
def test_same_group_value_cannot_replace_the_summary_threads_protected_witness(
    client, db_session, summaries, loss
):
    t = summaries
    original = command(client, t)
    accepted = send(client, t.w, original)
    accessible = command(client, t, record=add_record(db_session, t))
    left, right = (p["command"]["anchor"]["target"]["group"] for p in (original, accessible))
    assert left["group_id"] == right["group_id"] and left["witness_id"] != right["witness_id"]
    other = send(client, t.w, accessible)
    if loss == "read":
        deny_record(db_session, t)
    else:
        if loss == "delete":
            t.record.deleted = True
        else:
            t.record.collaboration_generation = uuid.uuid4()
        db_session.commit()
    tid, mid = accepted["thread_id"], accepted["message_id"]
    for suffix in (
        f"/threads/{tid}",
        f"/threads/{tid}/state",
        f"/threads/{tid}/messages/{mid}/history",
    ):
        get(client, t, suffix, expected=404)
    send(client, t.w, original, expected=404)
    send(
        client,
        t.w,
        envelope(
            t.w, dict(type="reply.create", thread_id=tid, message_id=str(uuid.uuid4()), body=body())
        ),
        expected=404,
    )
    assert [
        r["id"] for r in get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]
    ] == [other["thread_id"]]
    assert all(
        e.get("thread_id") != tid
        for e in get(client, t, "/changes", view_id=t.captured["view_id"])["events"]
    )


@pytest.mark.parametrize("change", ["summary", "grouping", "formula"])
def test_definition_changes_reject_new_creation_but_keep_original_thread_history(
    client, summaries, change
):
    t = summaries
    original, stale = command(client, t), command(client, t)
    accepted = send(client, t.w, original)
    save(t, **{change: {"summary": "Sum", "grouping": "other", "formula": "score * 3"}[change]})
    inspect(client, t.w)
    assert "base_target_definition_changed" in str(send(client, t.w, stale, expected=409))
    assert send(client, t.w, original) == accepted
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


def test_capability_is_opt_in_and_disabling_creation_preserves_history(
    client, summaries, monkeypatch
):
    t = summaries
    original, stale = command(client, t), command(client, t)
    accepted = send(client, t.w, original)

    def capabilities():
        return client.get(
            "/v1/collaboration/capabilities",
            params={"share_id": str(t.w.share.id)},
            headers=headers(t.w.tokens["reviewer"]),
        ).json()["discussion_base_targets"]

    assert "group_summary" in capabilities()
    monkeypatch.setattr(get_settings(), "collaboration_bases_group_summaries_enabled", False)
    assert "group_summary" not in capabilities() and "summary" in capabilities()
    assert "base_group_summaries_disabled" in str(send(client, t.w, stale, expected=409))
    assert send(client, t.w, original) == accepted
    get(client, t, f"/threads/{accepted['thread_id']}")


@pytest.mark.parametrize("patch", ["view", "witness", "value", "property", "nested"])
def test_group_summary_cannot_forge_its_nested_group_scope_or_witness(client, summaries, patch):
    t = summaries
    payload = command(client, t)
    anchor = payload["command"]["anchor"]
    if patch == "view":
        anchor["view_id"] = t.snapshot["views"][1]["id"]
    elif patch == "witness":
        anchor["target"]["group"]["witness_id"] = str(uuid.uuid4())
    elif patch == "value":
        anchor["target"]["group"]["value"] = {"type": "text", "value": "Forged"}
    elif patch == "property":
        anchor["target"]["property"]["name"] = "forged"
    else:
        anchor["target"]["group"] = copy.deepcopy(anchor["target"])
    send(
        client,
        t.w,
        payload,
        expected=409 if patch == "view" else 422 if patch in {"property", "nested"} else 404,
    )


def test_group_summary_picture_rechecks_witness_after_storage_io(
    client, db_session, summaries, storage
):
    t = summaries
    picture_id, _ = upload(client, t.w, raster())
    payload = command(client, t)
    payload["command"]["body"] = picture_body(picture_id)
    send(client, t.w, payload)
    get_picture(client, t.w, picture_id, actor="reviewer")
    storage.before_get = lambda: deny_record(db_session, t)
    get_picture(client, t.w, picture_id, actor="reviewer", suffix="/thumbnail", expected=404)


def test_group_summary_reattachment_keeps_the_complete_read_intersection(
    client, db_session, summaries, monkeypatch
):
    from test_discussion_reattachment import move

    monkeypatch.setattr(get_settings(), "collaboration_reattach_enabled", True)
    t = summaries
    original = command(client, t)
    accepted = send(client, t.w, original)
    same = command(client, t)["command"]["anchor"]
    moved = send(client, t.w, move(t.w, accepted, same))
    revision = moved["anchor_revision"]
    unrelated = command(client, t, record=add_record(db_session, t))["command"]["anchor"]
    whole_summary = copy.deepcopy(same)
    whole_summary["target"].pop("group")
    whole_summary["target"]["type"] = "summary"
    for target in (unrelated, whole_summary):
        result = send(client, t.w, move(t.w, accepted, target, revision), expected=409)
        assert result["error"]["message"]["code"] == "reattach_readers_changed"
    group_only = copy.deepcopy(same)
    group_only["target"] = same["target"]["group"]
    moved = send(client, t.w, move(t.w, accepted, group_only, revision))
    send(client, t.w, move(t.w, accepted, same, moved["anchor_revision"]))

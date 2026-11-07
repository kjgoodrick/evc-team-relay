import copy
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_base_discussions import anchor as base_anchor
from test_base_discussions import base as base_fixture
from test_base_discussions import command as base_command
from test_base_discussions import deny_record
from test_base_discussions import targets as targets_fixture
from test_base_groups import (
    add_record,
)
from test_base_groups import (
    capture as group_capture,
)
from test_base_groups import (
    groups as groups_fixture,
)
from test_base_groups import (
    request as group_request,
)
from test_base_groups import (
    thread as group_thread,
)
from test_discussions import base as discussion_path
from test_discussions import (
    count,
    create,
    enable_anchors,
    envelope,
    headers,
    image_command,
    image_file_command,
    proof,
    send,
    text_command,
)
from test_discussions import image_file_world as image_file_fixture
from test_discussions import world as world_fixture

from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service, discussion_service

world = world_fixture
targets = targets_fixture
groups = groups_fixture
image_file_world = image_file_fixture
# Base registry fixtures use the named `base` fixture; keep the route helper separate.
route = discussion_path
base = base_fixture


@pytest.fixture(autouse=True)
def enable_reattach(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_reattach_enabled", True)


def move(w, thread, anchor, revision=1):
    return envelope(
        w,
        {
            "type": "thread.reattach",
            "thread_id": thread["thread_id"],
            "expected_anchor_revision": revision,
            "anchor": anchor,
        },
    )


def text_target(client, w, monkeypatch):
    enable_anchors(monkeypatch)
    result = client.get(route(w) + "/anchor-context", headers=headers(w.tokens["reviewer"]))
    assert result.status_code == 200, result.text
    return text_command(w)["command"]["anchor"]


def history(client, w, thread, actor="reviewer", **params):
    return client.get(
        route(w) + f"/threads/{thread['thread_id']}/anchor-history",
        headers=headers(w.tokens[actor]),
        params=params,
    )


def test_commenter_reattaches_without_note_edit_preserves_state_messages_and_target_history(
    client, db_session, world, monkeypatch
):
    w = world
    thread = send(client, w, create(w))
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.state",
                "thread_id": thread["thread_id"],
                "expected_state_revision": 1,
                "state": "resolved",
            },
        ),
    )
    anchor = text_target(client, w, monkeypatch)
    validate = []
    monkeypatch.setattr(
        anchor_service, "validate", lambda command: validate.append(command) or proof(command)
    )
    command = move(w, thread, anchor)
    result = send(client, w, command)
    assert result["anchor_revision"] == 2 and result["state"] == "resolved"
    assert result["state_revision"] == 2 and result["changed"] is True
    assert count(db_session, m.DiscussionMessage) == 1
    assert count(db_session, m.DiscussionAnchorRevision) == 2
    rows = history(client, w, thread, limit=1).json()
    assert rows["has_more"] and rows["next_after"] == 1
    assert rows["revisions"][0]["anchor"] == {"version": 1, "type": "note"}
    rows = history(client, w, thread, after=1).json()
    assert rows["revisions"][0]["anchor"] == anchor
    assert rows["revisions"][0]["actor_id"] == str(w.reviewer.id)
    assert rows["revisions"][0]["created_at"]
    events = client.get(route(w) + "/changes", headers=headers(w.tokens["reader"])).json()["events"]
    assert events[-1]["kind"] == "thread.state" and events[-1]["data"]["anchor_revision"] == 2
    assert events[-1]["data"]["change_type"] == "thread.reattach"
    monkeypatch.setattr(get_settings(), "collaboration_reattach_enabled", False)
    assert send(client, w, command) == result
    assert len(validate) == 1
    assert history(client, w, thread, actor="reader").status_code == 200


def test_stale_and_same_target_saves_do_not_append_or_replace_history(
    client, db_session, world, monkeypatch
):
    w = world
    original = send(client, w, create(w))
    anchor = text_target(client, w, monkeypatch)
    monkeypatch.setattr(anchor_service, "validate", proof)
    send(client, w, move(w, original, anchor))
    result = send(client, w, move(w, original, anchor, revision=2))
    assert result["anchor_revision"] == 2 and not result["changed"]
    before = (count(db_session, m.DiscussionEvent), count(db_session, m.DiscussionReceipt))
    rejected = send(client, w, move(w, original, {"version": 1, "type": "note"}), expected=409)
    assert rejected["error"]["message"]["code"] == "stale_anchor_revision"
    assert before == (count(db_session, m.DiscussionEvent), count(db_session, m.DiscussionReceipt))
    assert count(db_session, m.DiscussionAnchorRevision) == 2


@pytest.mark.parametrize("case", ["reader", "scope", "removed", "disabled", "invalid_revision"])
def test_unauthorized_and_invalid_moves_never_call_private_relay(
    client, db_session, world, monkeypatch, case
):
    w = world
    thread = send(client, w, create(w))
    anchor = text_target(client, w, monkeypatch)
    called = []
    monkeypatch.setattr(
        anchor_service, "validate", lambda command: called.append(command) or proof(command)
    )
    command = move(w, thread, anchor)
    actor, expected = "reviewer", 409
    if case == "reader":
        actor, expected = "reader", 403
    if case == "scope":
        command["entry_id"] = w.other.id
        actor, expected = "owner", 404
    if case == "removed":
        row = db_session.get(m.DiscussionThread, uuid.UUID(thread["thread_id"]))
        row.deleted = True
        db_session.commit()
        expected = 404
    if case == "disabled":
        monkeypatch.setattr(get_settings(), "collaboration_reattach_enabled", False)
    if case == "invalid_revision":
        command["command"]["expected_anchor_revision"] = True
        expected = 422
    send(client, w, command, actor=actor, expected=expected)
    assert not called and count(db_session, m.DiscussionAnchorRevision) == 1


def test_collapsed_replacement_and_foreign_generation_are_rejected(
    client, db_session, world, monkeypatch
):
    w = world
    thread = send(client, w, create(w))
    anchor = text_target(client, w, monkeypatch)
    monkeypatch.setattr(
        anchor_service, "validate", lambda command: proof(command, placement="unanchored")
    )
    result = send(client, w, move(w, thread, anchor), expected=409)
    assert result["error"]["message"]["code"] == "reattach_target_unanchored"
    anchor["generation"] = str(uuid.uuid4())
    result = send(client, w, move(w, thread, anchor), expected=409)
    assert result["error"]["message"]["code"] == "anchor_generation_mismatch"
    assert count(db_session, m.DiscussionAnchorRevision) == 1


def test_authority_and_revision_are_rechecked_after_relay_io(db_session, world, monkeypatch):
    w = world
    enable_anchors(monkeypatch)
    token, reviewer_id = w.tokens["reviewer"], w.reviewer.id
    thread = discussion_service.apply_command(
        db_session, token, CommandEnvelope.model_validate(create(w))
    )
    discussion_service.document_context(db_session, token, w.share.id, w.entry.id)
    anchor = text_command(w)["command"]["anchor"]
    payload = CommandEnvelope.model_validate(move(w, thread, anchor))

    def validate(command):
        assert not db_session.in_transaction()
        rule = db_session.scalar(
            select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == reviewer_id)
        )
        rule.allowed = False
        db_session.commit()
        return proof(command)

    monkeypatch.setattr(anchor_service, "validate", validate)
    with pytest.raises(HTTPException) as rejected:
        discussion_service.apply_command(db_session, token, payload)
    assert rejected.value.status_code == 403
    assert count(db_session, m.DiscussionAnchorRevision) == 1
    assert count(db_session, m.DiscussionEvent) == 1


def test_failure_after_anchor_flush_rolls_back_anchor_counter_event_and_receipt(
    db_session, world, monkeypatch
):
    w = world
    enable_anchors(monkeypatch)
    token = w.tokens["reviewer"]
    thread = discussion_service.apply_command(
        db_session, token, CommandEnvelope.model_validate(create(w))
    )
    discussion_service.document_context(db_session, token, w.share.id, w.entry.id)
    anchor = text_command(w)["command"]["anchor"]
    monkeypatch.setattr(anchor_service, "validate", proof)
    discussion_service.apply_command(
        db_session, token, CommandEnvelope.model_validate(move(w, thread, anchor))
    )

    def failed_commit():
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(db_session, "commit", failed_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        discussion_service.apply_command(
            db_session,
            token,
            CommandEnvelope.model_validate(move(w, thread, {"version": 1, "type": "note"}, 2)),
        )
    assert count(db_session, m.DiscussionAnchorRevision) == 2
    assert count(db_session, m.DiscussionEvent) == 2
    assert count(db_session, m.DiscussionReceipt) == 2


def test_bases_reattachment_keeps_saved_view_and_record_access_boundary(
    client, db_session, targets
):
    t = targets
    thread = send(client, t.w, base_command(t))
    new = base_anchor(t)
    new["target"]["property"]["name"] = "priority"
    assert send(client, t.w, move(t.w, thread, new))["anchor_revision"] == 2
    assert send(client, t.w, move(t.w, thread, base_anchor(t, "item"), 2))["anchor_revision"] == 3
    result = send(client, t.w, move(t.w, thread, base_anchor(t, "view"), 3), expected=409)
    assert result["error"]["message"]["code"] == "reattach_readers_changed"
    result = send(client, t.w, move(t.w, thread, base_anchor(t, second=True), 3), expected=409)
    assert result["error"]["message"]["code"] == "reattach_scope_changed"
    deny_record(db_session, t)
    assert history(client, t.w, thread).status_code == 404
    send(client, t.w, move(t.w, thread, new, 3), expected=404)


def test_base_property_targets_can_move_within_the_same_view_without_changing_readers(
    client, targets
):
    t = targets
    thread = send(client, t.w, base_command(t, "property"))
    result = send(client, t.w, move(t.w, thread, base_anchor(t, "view")))
    assert result["anchor_revision"] == 2
    response = history(client, t.w, thread)
    assert response.status_code == 200
    assert [r["anchor"]["target"]["type"] for r in response.json()["revisions"]] == [
        "property",
        "view",
    ]


def test_direct_image_geometry_moves_keep_image_identity_and_need_no_text_relay(
    client, db_session, image_file_world, monkeypatch
):
    w = image_file_world
    thread = send(client, w, image_file_command(w, {"type": "whole"}))
    monkeypatch.setattr(
        anchor_service,
        "validate",
        lambda *_: pytest.fail("Direct image move called the text relay"),
    )
    target = image_file_command(w, {"type": "pin", "x": 0.1, "y": 0.9})["command"]["anchor"]
    assert send(client, w, move(w, thread, target))["anchor_revision"] == 2
    wrong = copy.deepcopy(target)
    wrong["asset"]["entry_id"] = w.other.id
    send(client, w, move(w, thread, wrong, 2), expected=422)
    send(client, w, move(w, thread, {"version": 1, "type": "note"}, 2), expected=422)
    assert history(client, w, thread).json()["revisions"][-1]["anchor"] == target
    assert count(db_session, m.DiscussionAnchorRevision) == 2


def test_embedded_image_move_uses_private_occurrence_proof_and_can_return_to_whole_note(
    client, world, monkeypatch
):
    w = world
    thread = send(client, w, create(w))
    text_target(client, w, monkeypatch)
    target = image_command(w)["command"]["anchor"]

    def validate(envelope):
        text_envelope = envelope.model_copy(deep=True)
        text_envelope.command.anchor = envelope.command.anchor.occurrence
        result = proof(text_envelope)
        return anchor_service.AnchorProof(
            **{**result.__dict__, "command_sha256": anchor_service.command_hash(envelope)}
        )

    monkeypatch.setattr(anchor_service, "validate", validate)
    assert send(client, w, move(w, thread, target))["anchor_revision"] == 2
    assert (
        send(client, w, move(w, thread, {"version": 1, "type": "note"}, 2))["anchor_revision"] == 3
    )
    assert [row["anchor"]["type"] for row in history(client, w, thread).json()["revisions"]] == [
        "note",
        "image",
        "note",
    ]


def test_group_reattachment_preserves_witness_read_dependencies(client, db_session, groups):
    t = groups
    original = group_capture(client, t, group_request(client, t))
    thread = send(client, t.w, group_thread(t, original))
    same_record = group_capture(client, t, group_request(client, t))
    assert send(client, t.w, move(t.w, thread, same_record["anchor"]))["anchor_revision"] >= 1
    latest = history(client, t.w, thread).json()["thread"]["anchor_revision"]
    other_record = add_record(db_session, t)
    changed_readers = group_capture(client, t, group_request(client, t, record=other_record))
    result = send(client, t.w, move(t.w, thread, changed_readers["anchor"], latest), expected=409)
    assert result["error"]["message"]["code"] == "reattach_readers_changed"
    result = send(client, t.w, move(t.w, thread, base_anchor(t, "view"), latest), expected=409)
    assert result["error"]["message"]["code"] == "reattach_readers_changed"


def test_unconfirmed_move_can_be_withdrawn_or_confirmed_without_a_new_relay_request(
    client, world, monkeypatch
):
    w = world
    thread = send(client, w, create(w))
    target = text_target(client, w, monkeypatch)
    command = move(w, thread, target)
    monkeypatch.setattr(
        anchor_service, "validate", lambda *_: pytest.fail("Withdrawal must not call private relay")
    )
    url = "/v1/collaboration/commands/withdraw"
    assert (
        client.post(url, headers=headers(w.tokens["reviewer"]), json=command).json()["status"]
        == "withdrawn"
    )
    result = send(client, w, command, expected=410)
    assert result["error"]["message"]["code"] == "operation_withdrawn"
    monkeypatch.setattr(anchor_service, "validate", proof)
    accepted = move(w, thread, target)
    result = send(client, w, accepted)
    monkeypatch.setattr(
        anchor_service, "validate", lambda *_: pytest.fail("Accepted retry must not call relay")
    )
    assert client.post(url, headers=headers(w.tokens["reviewer"]), json=accepted).json() == {
        "status": "accepted",
        "result": result,
    }


def test_history_reauthorizes_each_imported_target_instead_of_using_current_scope_access(
    client, db_session, targets
):
    t = targets
    created = send(client, t.w, base_command(t, "cell"))
    # Simulate retained/imported history whose older dependency differs from
    # its current view target. New reattach commands cannot create this state,
    # but history reads must not assume that every stored revision is readable.
    thread = db_session.get(m.DiscussionThread, uuid.UUID(created["thread_id"]))
    thread.anchor_revision = 2
    db_session.add(
        m.DiscussionAnchorRevision(
            thread_id=thread.id,
            revision=2,
            actor_id=t.w.owner.id,
            payload={**base_anchor(t, "view"), "entry_id": t.w.entry.id},
        )
    )
    db_session.commit()
    deny_record(db_session, t)
    response = history(client, t.w, created)
    assert response.status_code == 200, response.text
    revisions = response.json()["revisions"]
    assert revisions[0]["unavailable"] is True and revisions[0]["anchor"] is None
    assert revisions[1]["unavailable"] is False
    assert revisions[1]["anchor"]["target"]["type"] == "view"

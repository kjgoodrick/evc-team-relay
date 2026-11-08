import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from test_discussion_command_status import inspect
from test_discussions import create, enable_anchors, headers, send, text_command
from test_discussions import world as world_fixture

from app.db import models as m
from app.services import anchor_service

world = world_fixture


def request(client, w, payload, generation=None, *, withdraw=False, expected=409):
    auth = headers(w.tokens["reviewer"])
    if generation is not None:
        auth["X-Relay-Restore-Generation"] = str(generation)
    response = client.post(
        "/v1/collaboration/commands" + ("/withdraw" if withdraw else ""),
        json=payload,
        headers=auth,
    )
    assert response.status_code == expected, response.text
    if expected == 409:
        assert "restore_generation_changed" in response.text
    return response.json()


def test_original_lineage_accepts_legacy_commands_but_rejects_a_foreign_generation(
    client, db_session, world
):
    command = create(world)
    request(client, world, command, uuid.uuid4())
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 0
    assert request(client, world, command, expected=200)["operation_id"] == command["operation_id"]


@pytest.mark.parametrize("withdraw", [False, True])
def test_restore_requires_exact_generation_before_any_command_or_withdrawal(
    client, db_session, world, withdraw
):
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()
    command = create(world)
    for previous in (None, uuid.uuid4()):
        request(client, world, command, previous, withdraw=withdraw)
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 0
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionMessage)) == 0
    assert db_session.get(m.Share, world.share.id).collaboration_sequence == 0
    result = request(client, world, command, generation, withdraw=withdraw, expected=200)
    assert (
        result.get("status") == "withdrawn"
        if withdraw
        else result["operation_id"] == command["operation_id"]
    )


def test_old_accepted_receipt_can_be_inspected_but_not_replayed_without_the_generation(
    client, db_session, world
):
    command = create(world)
    accepted = send(client, world, command)
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    world.share.collaboration_epoch = uuid.uuid4()
    db_session.commit()
    request(client, world, command)
    request(client, world, command, withdraw=True)
    assert inspect(client, world, command) == dict(status="accepted", result=accepted)
    # Generation is transport context, not part of immutable request identity.
    assert request(client, world, command, generation, expected=200) == accepted
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 1
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionEvent)) == 1


def test_ordinary_cursor_reset_does_not_invalidate_queued_work(client, db_session, world):
    world.share.collaboration_epoch = uuid.uuid4()
    db_session.commit()
    request(client, world, create(world), expected=200)
    assert db_session.get(m.Share, world.share.id).collaboration_restore_generation is None


def test_anchor_preflight_rejects_old_generation_without_contacting_relay(
    client, db_session, world, monkeypatch
):
    enable_anchors(monkeypatch)
    world.entry.collaboration_generation = uuid.uuid4()
    world.share.collaboration_restore_generation = uuid.uuid4()
    db_session.commit()
    validate = Mock(side_effect=AssertionError("Old request must not reach private relay"))
    monkeypatch.setattr(anchor_service, "validate", validate)
    request(client, world, text_command(world))
    validate.assert_not_called()


def test_generation_change_during_anchor_validation_prevents_final_commit(
    client, db_session, world, monkeypatch
):
    enable_anchors(monkeypatch)
    world.entry.collaboration_generation = uuid.uuid4()
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()

    def rotate(_envelope):
        share = db_session.get(m.Share, world.share.id)
        share.collaboration_restore_generation = uuid.uuid4()
        db_session.commit()
        return object()  # Final generation check must run before using proof.

    monkeypatch.setattr(anchor_service, "validate", rotate)
    request(client, world, text_command(world), generation)
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 0
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionThread)) == 0


def test_capability_reports_current_generation_and_missing_access_stays_hidden(
    client, db_session, world
):
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()
    response = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(world.share.id)},
        headers=headers(world.tokens["reviewer"]),
    )
    assert response.status_code == 200
    assert response.json()["restore_generation"] == str(generation)
    assert response.json()["features"]["discussion_command_restore_guard"] is True
    hidden = create(world)
    hidden["entry_id"] = world.other.id
    request(client, world, hidden, expected=404)

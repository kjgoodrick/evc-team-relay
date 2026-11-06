import copy
import uuid

import pytest
from sqlalchemy import func, select
from test_discussion_pictures import raster, stage
from test_discussion_pictures import storage as picture_storage
from test_discussions import create, headers, send
from test_discussions import world as discussion_world

from app.db import models as m

world = discussion_world
storage = picture_storage


def withdraw(client, w, payload, *, actor="reviewer", expected=200):
    response = client.post(
        "/v1/collaboration/commands/withdraw", json=payload, headers=headers(w.tokens[actor])
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_withdrawal_retries_are_exact_and_late_send_cannot_commit(client, db_session, world):
    command = create(world)
    expected = dict(
        status="withdrawn", operation_id=command["operation_id"], entry_id=command["entry_id"]
    )
    assert withdraw(client, world, command) == expected
    assert withdraw(client, world, command) == expected
    rejected = send(client, world, command, expected=410)
    assert "operation_withdrawn" in str(rejected)
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 1
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionMessage)) == 0
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionEvent)) == 0
    changed = copy.deepcopy(command)
    changed["command"]["body"]["blocks"][0]["children"][0]["text"] = "Changed"
    withdraw(client, world, changed, expected=409)
    send(client, world, changed, expected=409)


def test_accepted_receipt_wins_even_after_message_edits(client, world):
    command = create(world)
    accepted = send(client, world, command)
    assert withdraw(client, world, command) == dict(status="accepted", result=accepted)
    assert send(client, world, command) == accepted


def test_withdrawal_requires_current_comment_access_and_does_not_reveal_other_actors(client, world):
    command = create(world)
    send(client, world, command)
    withdraw(client, world, command, actor="reader", expected=403)
    # A different actor cannot retire the actual author's receipt or discover it.
    assert withdraw(client, world, command, actor="owner")["status"] == "withdrawn"
    assert withdraw(client, world, command)["status"] == "accepted"
    private = create(world)
    private["entry_id"] = world.other.id
    withdraw(client, world, private, expected=404)


def test_recovery_does_not_require_picture_upload_or_validate_the_body_target(
    client, world, storage
):
    payload, _ = stage(client, world, raster())
    command = create(world)
    command["command"]["body"] = dict(
        version=1, blocks=[dict(type="picture", picture_id=payload["picture_id"])]
    )
    assert withdraw(client, world, command)["status"] == "withdrawn"
    assert storage.puts == 0
    send(client, world, command, expected=410)


def test_recovery_does_not_disclose_an_accepted_receipt_after_revocation(client, db_session, world):
    command = create(world)
    send(client, world, command)
    rule = db_session.scalar(
        select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == world.reviewer.id)
    )
    rule.allowed = False
    db_session.commit()
    withdraw(client, world, command, expected=403)


@pytest.mark.parametrize("action", ["reply.create", "message.edit", "reaction.set"])
def test_withdrawal_fences_unaccepted_queued_actions_without_requiring_target_to_exist(
    client, world, action
):
    envelope = create(world)
    command = envelope["command"]
    command.pop("anchor")
    command["type"] = action
    if action == "message.edit":
        command["expected_revision"] = 1
    if action == "reaction.set":
        command.pop("body")
        command.update(emoji="thumbs_up", present=True)
    assert withdraw(client, world, envelope)["status"] == "withdrawn"
    send(client, world, envelope, expected=410)


def test_withdrawal_preserves_scope_and_operation_identity(client, world):
    command = create(world)
    withdraw(client, world, command)
    changed = copy.deepcopy(command)
    changed["command"]["message_id"] = str(uuid.uuid4())
    withdraw(client, world, changed, expected=409)

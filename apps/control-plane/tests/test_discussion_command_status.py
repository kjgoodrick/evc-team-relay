import copy
import uuid

import pytest
from sqlalchemy import func, select
from test_base_discussions import base as base_fixture
from test_base_discussions import command as base_command
from test_base_discussions import deny_record
from test_base_discussions import targets as targets_fixture
from test_discussion_recovery import withdraw
from test_discussions import create, headers, send
from test_discussions import world as world_fixture

from app.db import models as m

world = world_fixture
base = base_fixture
targets = targets_fixture


def inspect(client, world, command, actor="reviewer", expected=200):
    response = client.post(
        "/v1/collaboration/commands/status", json=command, headers=headers(world.tokens[actor])
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.headers["cache-control"] == "no-store"
    return response.json()


def counts(db):
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (
            m.DiscussionScope,
            m.DiscussionReceipt,
            m.DiscussionThread,
            m.DiscussionMessage,
            m.DiscussionEvent,
        )
    )


def test_unknown_does_not_write_a_receipt_or_prevent_a_later_explicit_send(
    client, db_session, world
):
    command = create(world)
    before = counts(db_session)
    expected = dict(
        status="unknown", operation_id=command["operation_id"], entry_id=command["entry_id"]
    )
    assert inspect(client, world, command) == expected
    assert inspect(client, world, command) == expected
    assert counts(db_session) == before
    accepted = send(client, world, command)
    before = counts(db_session)
    assert inspect(client, world, command) == dict(status="accepted", result=accepted)
    assert counts(db_session) == before


def test_withdrawn_receipt_remains_withdrawn_without_new_writes(client, db_session, world):
    command = create(world)
    result = withdraw(client, world, command)
    before = counts(db_session)
    assert inspect(client, world, command) == result
    assert counts(db_session) == before
    send(client, world, command, expected=410)


def test_receipt_read_needs_read_access_but_no_longer_needs_comment_access(
    client, db_session, world
):
    command = create(world)
    accepted = send(client, world, command)
    rule = db_session.scalar(
        select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == world.reviewer.id)
    )
    rule.allowed = False
    db_session.commit()
    assert inspect(client, world, command) == dict(status="accepted", result=accepted)
    withdraw(client, world, command, expected=403)
    read_rule = db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
    )
    read_rule.permission = "none"
    db_session.commit()
    inspect(client, world, command, expected=404)


def test_other_actors_cannot_discover_or_change_a_receipt(client, db_session, world):
    command = create(world)
    send(client, world, command)
    before = counts(db_session)
    for actor in ("owner", "reader"):
        assert inspect(client, world, command, actor=actor)["status"] == "unknown"
    assert counts(db_session) == before


@pytest.mark.parametrize("change", ["body", "operation", "entry"])
def test_exact_request_and_scope_identity_are_required(client, world, change):
    command = create(world)
    send(client, world, command)
    other = copy.deepcopy(command)
    if change == "body":
        other["command"]["body"]["blocks"][0]["children"][0]["text"] = "different"
        inspect(client, world, other, expected=409)
    elif change == "operation":
        other["operation_id"] = str(uuid.uuid4())
        assert inspect(client, world, other)["status"] == "unknown"
    else:
        other["entry_id"] = world.other.id
        inspect(client, world, other, expected=404)


def test_missing_receipt_after_restore_is_unknown_and_never_resubmits(client, db_session, world):
    command = create(world)
    send(client, world, command)
    receipt = db_session.get(
        m.DiscussionReceipt, (world.reviewer.id, uuid.UUID(command["operation_id"]))
    )
    db_session.delete(receipt)
    db_session.commit()
    before = counts(db_session)
    assert inspect(client, world, command)["status"] == "unknown"
    assert counts(db_session) == before


def test_saved_view_receipt_rechecks_record_read_dependencies(client, db_session, targets):
    command = base_command(targets)
    accepted = send(client, targets.w, command)
    assert inspect(client, targets.w, command) == dict(status="accepted", result=accepted)
    deny_record(db_session, targets, read=False)
    assert inspect(client, targets.w, command) == dict(status="accepted", result=accepted)
    deny_record(db_session, targets)
    inspect(client, targets.w, command, expected=404)


def test_invalid_request_does_not_echo_body(client, world):
    command = create(world, "private-status-probe-body")
    command["command"]["type"] = "invalid"
    result = inspect(client, world, command, expected=422)
    assert "private-status-probe-body" not in str(result)

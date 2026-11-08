import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_base_discussions import base as base_fixture
from test_base_discussions import command
from test_base_discussions import targets as targets_fixture
from test_discussion_moderation import enable as enable_fixture
from test_discussion_moderation import recover, remove
from test_discussions import base as route
from test_discussions import create, envelope, headers, send
from test_discussions import world as world_fixture

from app.core import security
from app.core.config import get_settings
from app.db import models as m

world, base, targets, enable = world_fixture, base_fixture, targets_fixture, enable_fixture


def catalog(client, w, *, actor="owner", expected=200, **data):
    response = client.post(
        route(w) + "/moderation-catalog",
        headers=headers(w.tokens[actor]),
        json={"reason": "Investigate synthetic removal", **data},
    )
    assert response.status_code == expected, response.text
    assert "Retained private history" not in response.text
    assert "Synthetic thread removal" not in response.text
    if expected == 200:
        assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_catalog_pages_removed_messages_and_threads_without_content_and_audits(
    client, db_session, world
):
    w = world
    live = send(client, w, create(w))
    first, second = remove(client, w), remove(client, w, thread=True)
    page = catalog(client, w, limit=1)
    assert page["entry_id"] == w.entry.id and page["view_id"] is None
    assert page["has_more"] and len(page["items"]) == 1
    assert page["items"][0]["message_id"] == first["message_id"]
    assert page["items"][0]["message_removed"] and not page["items"][0]["thread_removed"]
    assert not ({"body", "reason", "anchor"} & page["items"][0].keys())
    audit = db_session.get(m.AuditLog, uuid.UUID(page["audit_id"]))
    assert audit.details["resource"] == "removed_catalog"
    assert audit.details["messages"] == [
        {"thread_id": first["thread_id"], "message_id": first["message_id"]}
    ]
    assert live["message_id"] not in str(audit.details)
    next_page = catalog(client, w, after=page["next_after"], limit=1)
    assert not next_page["has_more"] and next_page["items"][0]["message_id"] == second["message_id"]
    assert next_page["items"][0]["thread_removed"] and not next_page["items"][0]["message_removed"]
    assert next_page["audit_id"] != page["audit_id"]


@pytest.mark.parametrize("actor", ["reader", "reviewer"])
def test_catalog_requires_current_moderator_not_authorship(client, world, actor):
    remove(client, world)
    catalog(client, world, actor=actor, expected=403)


@pytest.mark.parametrize("unavailable", ["expired", "missing_clock", "purged"])
def test_unavailable_candidates_do_not_stop_pagination(client, db_session, world, unavailable):
    first, second = remove(client, world), remove(client, world)
    row = db_session.get(m.DiscussionMessage, uuid.UUID(first["message_id"]))
    if unavailable == "expired":
        row.deleted_at = security.utcnow() - timedelta(days=31)
    elif unavailable == "missing_clock":
        row.deleted_at = None
    else:
        row.content_purged_at = security.utcnow()
    db_session.commit()
    page = catalog(client, world, limit=1)
    if unavailable != "purged":
        assert page["items"] == [] and page["has_more"]
        page = catalog(client, world, after=page["next_after"], limit=1)
    assert page["items"][0]["message_id"] == second["message_id"]


def test_catalog_saved_view_isolation_and_current_record_authority(client, db_session, targets):
    t, w = targets, targets.w
    made = []
    for second in (False, True):
        item = send(client, w, command(t, second=second))
        send(
            client,
            w,
            envelope(
                w,
                dict(
                    type="thread.remove",
                    thread_id=item["thread_id"],
                    expected_state_revision=1,
                    reason="Synthetic removal",
                ),
            ),
            actor="owner",
        )
        made.append(item)
    view_id = t.captured["view_id"]
    page = catalog(client, w, view_id=view_id)
    assert [item["message_id"] for item in page["items"]] == [made[0]["message_id"]]
    catalog(client, w, expected=422)
    recovered = recover(client, w, made[0], view_id=view_id)
    assert recovered["entry_id"] == w.entry.id and recovered["view_id"] == view_id
    recover(client, w, made[0], view_id=t.snapshot["views"][1]["id"], expected=404)
    # Even the owner cannot use a deleted dependency to authorize recovery.
    t.record.deleted = True
    db_session.commit()
    page = catalog(client, w, view_id=view_id)
    assert page["items"] == []


def test_catalog_failed_audit_discloses_no_target_and_flag_gates_reads(
    client, db_session, world, monkeypatch
):
    made = remove(client, world)
    before = db_session.scalar(select(func.count()).select_from(m.AuditLog))
    with monkeypatch.context() as patch:

        def fail(self):
            raise OperationalError("synthetic catalog audit failure", {}, None)

        patch.setattr(Session, "commit", fail)
        result = catalog(client, world, expected=503)
        assert made["message_id"] not in str(result)
    assert db_session.scalar(select(func.count()).select_from(m.AuditLog)) == before
    monkeypatch.setattr(get_settings(), "collaboration_moderation_recovery_enabled", False)
    catalog(client, world, expected=404)


@pytest.mark.parametrize(
    "data",
    [{"reason": ""}, {"reason": "x\nsecret"}, {"after": -1}, {"limit": 51}, {"view_id": "invalid"}],
)
def test_catalog_rejects_invalid_review_input(client, world, data):
    catalog(client, world, expected=422, **data)

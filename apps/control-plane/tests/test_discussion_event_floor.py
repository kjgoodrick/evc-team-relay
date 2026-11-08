import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from test_discussions import base, create, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m

world = discussion_world


def changes(client, w, expected=200, **params):
    response = client.get(base(w) + "/changes", params=params, headers=headers(w.tokens["reader"]))
    assert response.status_code == expected, response.text
    return response.json()


def advance_fixture(db, w, sequence):
    # Owned test data only. No production pruning worker is enabled by this.
    db.execute(
        update(m.Share).where(m.Share.id == w.share.id).values(discussion_event_floor=sequence)
    )
    db.execute(
        delete(m.DiscussionEvent).where(
            m.DiscussionEvent.share_id == w.share.id, m.DiscussionEvent.sequence <= sequence
        )
    )
    db.commit()


def test_retained_tail_cannot_masquerade_as_complete_history(
    client, world, db_session, monkeypatch
):
    w = world
    monkeypatch.setattr(get_settings(), "collaboration_snapshot_enabled", True)
    for index in range(3):
        send(client, w, create(w, f"Current discussion {index}"))
    old = changes(client, w, limit=1)["cursor"]
    boundary = changes(client, w, limit=2)["cursor"]
    advance_fixture(db_session, w, 2)
    for params in ({}, {"cursor": old}):
        assert (
            changes(client, w, expected=426, **params)["error"]["message"]["code"]
            == "discussion_sync_upgrade_required"
        )
        assert (
            changes(client, w, expected=409, sync_protocol=1, **params)["error"]["message"]["code"]
            == "snapshot_required"
        )
    for params in ({}, {"sync_protocol": 1}):
        tail = changes(client, w, cursor=boundary, **params)
        assert [event["sequence"] for event in tail["events"]] == [3]
    response = client.get(base(w) + "/snapshot", headers=headers(w.tokens["reader"]))
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert len(snapshot["threads"]) == 3 and snapshot["through_sequence"] == 3
    assert changes(client, w, cursor=snapshot["cursor"], sync_protocol=1)["events"] == []


def test_disabled_snapshot_does_not_make_pruned_events_replayable(
    client, world, db_session, monkeypatch
):
    w = world
    send(client, w, create(w))
    advance_fixture(db_session, w, 1)
    monkeypatch.setattr(get_settings(), "collaboration_snapshot_enabled", False)
    assert (
        changes(client, w, expected=409, sync_protocol=1)["error"]["message"]["code"]
        == "snapshot_required"
    )
    response = client.get(base(w) + "/snapshot", headers=headers(w.tokens["reader"]))
    assert response.status_code == 404


def test_permissions_are_checked_before_replay_admission(client, world, db_session):
    w = world
    send(client, w, create(w))
    advance_fixture(db_session, w, 1)
    response = client.get(
        base(w).replace(w.entry.id, w.other.id) + "/changes", headers=headers(w.tokens["reader"])
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"]["code"] == "target_unavailable"


@pytest.mark.parametrize("floor", [-1, 2])
def test_invalid_retention_boundary_is_rejected_by_database(client, world, db_session, floor):
    w = world
    send(client, w, create(w))
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(
            update(m.Share).where(m.Share.id == w.share.id).values(discussion_event_floor=floor)
        )


def test_zero_floor_keeps_legacy_initial_replay(client, world):
    w = world
    made = send(client, w, create(w))
    for params in ({}, {"sync_protocol": 1}):
        page = changes(client, w, **params)
        assert [event["sequence"] for event in page["events"]] == [made["sequence"]]

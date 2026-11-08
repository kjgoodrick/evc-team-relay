import copy
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from test_base_discussions import get
from test_base_discussions import targets as target_fixture
from test_bases_registry import base as base_fixture
from test_bases_registry import inspect, path, view
from test_discussions import create, headers, send
from test_discussions import world as discussion_world
from test_timeline_range_discussions import RANGES, axis_context, configure, reply
from test_timeline_range_discussions import timeline as timeline_fixture

from app.core.config import get_settings
from app.db import models as m
from app.schemas.timeline_markers import TimelineMarkerCommand
from app.services import timeline_markers as service

world, base, targets, timeline = (discussion_world, base_fixture, target_fixture, timeline_fixture)


@pytest.fixture
def markers(timeline, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_markers_enabled", True)
    return timeline


def marker_path(t):
    return path(t.w) + "/timeline-markers"


def catalog(client, t, *, actor="reviewer", expected=200, all_views=False, **payload):
    response = client.post(
        marker_path(t) + "/catalog",
        headers=headers(t.w.tokens[actor]),
        json={**({} if all_views else {"view_id": t.captured["view_id"]}), **payload},
    )
    assert response.status_code == expected, response.text
    return response.json()


def put(client, t, *, marker=None, kind="calendar", views=None):
    page = catalog(client, t)
    views = views or [t.captured["view_id"]]
    axes = [axis_context(client, t, view_id=view_id) for view_id in views]
    return dict(
        operation_id=str(uuid.uuid4()),
        **{key: page[key] for key in ("definition_id", "generation", "registry_revision")},
        expected_catalog_revision=page["catalog_revision"],
        command=dict(
            type="marker.put",
            marker_id=marker["id"] if marker else str(uuid.uuid4()),
            expected_marker_revision=marker["revision"] if marker else 0,
            content={
                "kind": "range",
                "label": "Shared launch",
                "color": "#1A2B3C",
                "range": copy.deepcopy(
                    next(case["input"] for case in RANGES if case["input"]["kind"] == kind)
                ),
            },
            placements=[
                dict(
                    view_id=axis["view_id"],
                    axis_definition_id=axis["axis"]["id"],
                    axis_definition_revision=axis["axis"]["revision"],
                )
                for axis in axes
            ],
        ),
    )


def mutate(client, t, payload, *, actor="owner", expected=200):
    response = client.post(
        marker_path(t) + "/commands", headers=headers(t.w.tokens[actor]), json=payload
    )
    assert response.status_code == expected, response.text
    return response.json()


def thread(client, t, *, view_id=None, item=None):
    page = catalog(client, t, **({"view_id": view_id} if view_id else {}))
    result = create(t.w)
    result["command"]["anchor"] = dict(
        version=1,
        type="base",
        **{key: page[key] for key in ("definition_id", "generation", "registry_revision")},
        view_id=view_id or t.captured["view_id"],
        target=copy.deepcopy((item or page["markers"][0])["target"]),
    )
    return result


def archive(client, t, marker):
    payload = put(client, t, marker=marker)
    payload["command"] = dict(
        type="marker.archive", marker_id=marker["id"], expected_marker_revision=marker["revision"]
    )
    return payload


def test_restore_guards_marker_create_edit_archive_and_exact_retry(client, db_session, markers):
    t = markers
    initial = put(client, t)
    accepted = mutate(client, t, initial)
    edit = put(client, t, marker=accepted["marker"])
    remove = archive(client, t, accepted["marker"])
    create_new = put(client, t)
    generation = uuid.uuid4()
    t.w.share.collaboration_restore_generation = generation
    db_session.commit()
    original = catalog(client, t)
    auth = headers(t.w.tokens["owner"])
    for previous in (None, uuid.uuid4()):
        stale = dict(auth)
        if previous:
            stale["X-Relay-Restore-Generation"] = str(previous)
        for payload in (initial, edit, remove, create_new):
            response = client.post(marker_path(t) + "/commands", json=payload, headers=stale)
            assert response.status_code == 409 and "restore_generation_changed" in response.text
    assert catalog(client, t) == original
    assert db_session.scalar(select(func.count()).select_from(m.BaseTimelineMarkerReceipt)) == 1
    auth["X-Relay-Restore-Generation"] = str(generation)
    response = client.post(marker_path(t) + "/commands", json=initial, headers=auth)
    assert response.status_code == 200 and response.json() == accepted
    response = client.post(marker_path(t) + "/commands", json=remove, headers=auth)
    assert response.status_code == 200 and response.json()["marker"]["active"] is False
    # Guard remains advertised for exact receipt recovery if new marker writes stop.
    get_settings().collaboration_bases_timeline_markers_enabled = False
    response = client.post(marker_path(t) + "/commands", json=remove, headers=auth)
    assert response.status_code == 200
    response = client.get(f"/v1/collaboration/capabilities?share_id={t.w.share.id}", headers=auth)
    assert response.json()["features"]["discussion_timeline_marker_restore_guard"] is True


def test_changes_opt_in_reports_current_marker_revision_without_thread_events(
    client, markers, monkeypatch
):
    t = markers
    params = {"view_id": t.captured["view_id"]}
    legacy = get(client, t, "/changes", **params)
    assert "timeline_markers" not in legacy
    initial = get(client, t, "/changes", **params, timeline_markers=True)
    assert initial["timeline_markers"]["catalog_revision"] == 0
    assert initial["timeline_markers"]["view_id"] == params["view_id"]
    assert initial["timeline_markers"]["entry_id"] == t.w.entry.id
    created = mutate(client, t, put(client, t))
    changed = get(client, t, "/changes", **params, cursor=initial["cursor"], timeline_markers=True)
    assert changed["events"] == []
    assert changed["timeline_markers"]["catalog_revision"] == created["catalog_revision"]
    assert set(changed["timeline_markers"]) == {
        "protocol_version",
        "entry_id",
        "view_id",
        "definition_id",
        "generation",
        "registry_revision",
        "catalog_revision",
    }
    archived = mutate(client, t, archive(client, t, created["marker"]))
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_markers_enabled", False)
    latest = get(client, t, "/changes", actor="reader", **params, timeline_markers=True)
    assert latest["timeline_markers"]["catalog_revision"] == archived["catalog_revision"]
    assert "timeline_markers" not in get(client, t, "/changes", **params)
    get(client, t, "/changes", view_id=str(uuid.uuid4()), timeline_markers=True, expected=404)


@pytest.mark.parametrize("kind", ["calendar", "instant", "historical", "ordinal"])
def test_shared_markers_preserve_exact_content_without_editing_base_or_tasks(
    client, db_session, markers, kind
):
    t = markers
    if kind == "ordinal":
        configure(client, t, sequence=True)
    payload = put(client, t, kind=kind)
    before = dict(db_session.get(m.ManagedEntry, t.w.entry.id).record)
    saved = mutate(client, t, payload)
    assert saved["marker"]["content"]["range"] == next(
        case["normalized"] for case in RANGES if case["input"]["kind"] == kind
    )
    assert saved["marker"]["content"]["color"] == "#1a2b3c"
    assert saved["marker"]["id"] == payload["command"]["marker_id"]
    assert saved["marker"]["revision"] == 1
    assert mutate(client, t, payload) == saved
    assert inspect(client, t.w)["revision"] == payload["registry_revision"]
    db_session.expire_all()
    assert db_session.get(m.ManagedEntry, t.w.entry.id).record == before
    assert catalog(client, t, actor="reader") == catalog(client, t)
    original = thread(client, t)
    accepted = send(client, t.w, original)
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == original["command"]["anchor"]["target"]
    )
    assert send(client, t.w, original) == accepted
    send(client, t.w, thread(client, t), actor="reader", expected=403)
    caps = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reader"]),
    ).json()
    assert caps["features"]["discussion_timeline_markers"] is True
    assert "timeline_marker" in caps["discussion_base_targets"]


def test_only_base_editors_can_promote_edit_archive_or_replay_management(
    client, db_session, markers
):
    t = markers
    payload = put(client, t)
    for actor in ("reviewer", "reader"):
        mutate(client, t, payload, actor=actor, expected=403)
    # An ordinary member with base edit permission can manage shared markers;
    # unrelated note access and comment permission are not editing authority.
    rule = m.ManagedRule(
        share_id=t.w.share.id,
        user_id=t.w.reviewer.id,
        path=t.w.entry.path,
        recursive=False,
        permission="write",
    )
    db_session.add(rule)
    db_session.commit()
    saved = mutate(client, t, payload, actor="reviewer")
    assert saved["marker"]["revision"] == 1
    rule.permission = "read"
    db_session.commit()
    mutate(client, t, payload, actor="reviewer", expected=403)
    mutate(client, t, put(client, t, marker=saved["marker"]), actor="reviewer", expected=403)
    mutate(client, t, archive(client, t, saved["marker"]), actor="reviewer", expected=403)
    send(client, t.w, thread(client, t))


def test_marker_moves_and_archives_preserve_history_and_old_receipts(client, db_session, markers):
    t = markers
    create_marker = put(client, t)
    first = mutate(client, t, create_marker)
    original = thread(client, t)
    accepted = send(client, t.w, original)
    stale = thread(client, t)
    update = put(client, t, marker=first["marker"])
    update["command"]["content"].update(label="Moved launch", color="#abcdef")
    update["command"]["content"]["range"].update(start="2027-03-01", end="2027-03-02")
    second = mutate(client, t, update)
    assert second["marker"]["id"] == first["marker"]["id"]
    assert second["marker"]["revision"] == 2
    send(client, t.w, stale, expected=409)
    assert send(client, t.w, original) == accepted
    assert mutate(client, t, create_marker) == first
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == original["command"]["anchor"]["target"]
    )
    send(client, t.w, reply(t, accepted["thread_id"]))
    removed = mutate(client, t, archive(client, t, second["marker"]))
    assert removed["marker"]["active"] is False
    assert catalog(client, t)["markers"] == []
    assert catalog(client, t, include_archived=True)["markers"][0]["revision"] == 3
    assert send(client, t.w, original) == accepted
    send(client, t.w, reply(t, accepted["thread_id"]))
    restored = mutate(client, t, put(client, t, marker=removed["marker"]))
    assert restored["marker"]["revision"] == 4
    send(client, t.w, thread(client, t))
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(m.BaseTimelineMarkerRevision)) == 4


def test_shared_marker_has_explicit_view_local_threads_and_retains_detached_history(
    client, markers
):
    t = markers
    t.base.save(
        [
            view(name, "sabidurian-timeline", startDateProp="note.start", timeZone="America/Denver")
            for name in ("Table", "Cards")
        ]
    )
    registry = inspect(client, t.w)
    view_ids = [item["id"] for item in registry["views"]]
    saved = mutate(client, t, put(client, t, views=view_ids))
    originals = [thread(client, t, view_id=view_id) for view_id in view_ids]
    receipts = [send(client, t.w, payload) for payload in originals]
    for view_id, receipt in zip(view_ids, receipts):
        page = get(client, t, "/threads", view_id=view_id)
        assert [item["id"] for item in page["threads"]] == [receipt["thread_id"]]
    mutate(client, t, put(client, t, marker=saved["marker"], views=[view_ids[0]]))
    assert catalog(client, t, view_id=view_ids[1])["markers"] == []
    archived = catalog(client, t, view_id=view_ids[1], include_archived=True)["markers"]
    assert archived[0]["placement"]["active"] is False
    assert send(client, t.w, originals[1]) == receipts[1]
    send(client, t.w, reply(t, receipts[1]["thread_id"]))
    forged = thread(client, t)
    forged["command"]["anchor"]["view_id"] = view_ids[1]
    send(client, t.w, forged, expected=409)


def test_promotion_cas_and_operation_identity_prevent_duplicate_writes(client, db_session, markers):
    t = markers
    first, simultaneous = put(client, t), put(client, t)
    receipt = mutate(client, t, first)
    rejected = mutate(client, t, simultaneous, expected=409)
    assert rejected["error"]["message"]["code"] == "timeline_marker_catalog_changed"
    changed = copy.deepcopy(first)
    changed["command"]["content"]["label"] = "Different intent"
    mutate(client, t, changed, expected=409)
    assert mutate(client, t, first) == receipt
    db_session.expire_all()
    for model in (m.BaseTimelineMarker, m.BaseTimelineMarkerReceipt, m.BaseTimelineMarkerRevision):
        assert db_session.scalar(select(func.count()).select_from(model)) == 1
    catalog(client, t, expected=409, expected_revision=0)
    catalog(client, t, expected=422, after=receipt["marker"]["id"])


def test_axis_edit_and_disable_fence_new_captures_without_erasing_marker_threads(
    client, markers, monkeypatch
):
    t = markers
    first = put(client, t)
    receipt = mutate(client, t, first)
    original = thread(client, t)
    accepted = send(client, t.w, original)
    stale = thread(client, t)
    configure(client, t, startDateProp="note.other")
    send(client, t.w, stale, expected=409)
    assert send(client, t.w, original) == accepted
    rebound = mutate(client, t, put(client, t, marker=receipt["marker"]))
    fresh = thread(client, t)
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_markers_enabled", False)
    send(client, t.w, fresh, expected=409)
    assert send(client, t.w, original) == accepted
    assert mutate(client, t, first) == receipt
    mutate(client, t, put(client, t, marker=rebound["marker"]), expected=409)
    assert catalog(client, t)["markers"][0]["revision"] == 2


def test_forged_content_placement_and_point_shapes_are_rejected(client, markers):
    t = markers
    payload = put(client, t)
    for key, value in (
        ("label", ""),
        ("label", "line\nbreak"),
        ("color", "url(https://example.test)"),
        ("kind", "unknown"),
    ):
        invalid = copy.deepcopy(payload)
        invalid["command"]["content"][key] = value
        mutate(client, t, invalid, expected=422)
    invalid = copy.deepcopy(payload)
    invalid["command"]["content"]["kind"] = "point"
    mutate(client, t, invalid, expected=422)
    invalid = copy.deepcopy(payload)
    invalid["command"]["placements"] *= 2
    mutate(client, t, invalid, expected=422)
    invalid = copy.deepcopy(payload)
    invalid["command"]["placements"][0]["axis_definition_id"] = str(uuid.uuid4())
    mutate(client, t, invalid, expected=409)
    saved = mutate(client, t, payload)
    forged = thread(client, t)
    forged["command"]["anchor"]["target"]["content"]["label"] = "Forged marker"
    send(client, t.w, forged, expected=422)
    detail = client.get(
        marker_path(t) + "/" + saved["marker"]["id"], headers=headers(t.w.tokens["reader"])
    )
    assert detail.status_code == 200
    assert detail.json()["marker"] == saved["marker"]


@pytest.mark.parametrize("kind", ["calendar", "instant", "historical", "ordinal"])
def test_point_markers_retain_explicit_coordinate_precision(client, markers, kind):
    t = markers
    if kind == "ordinal":
        configure(client, t, sequence=True)
    payload = put(client, t, kind=kind)
    content = payload["command"]["content"]
    content["kind"] = "point"
    content["range"]["end"] = copy.deepcopy(content["range"]["start"])
    content["range"].update(start_inclusive=True, end_inclusive=True)
    saved = mutate(client, t, payload)["marker"]["content"]
    assert saved["kind"] == "point" and saved["range"]["start"] == saved["range"]["end"]
    send(client, t.w, thread(client, t))


def test_marker_catalog_pagination_fences_concurrent_edits_and_bounds_creation(
    client, markers, monkeypatch
):
    t = markers
    monkeypatch.setattr(service, "PAGE_SIZE", 2)
    monkeypatch.setattr(service, "MAX_MARKERS", 3)
    saved = [mutate(client, t, put(client, t))["marker"] for _ in range(3)]
    first = catalog(client, t)
    assert len(first["markers"]) == 2 and first["has_more"]
    second = catalog(
        client, t, after=first["next_after"], expected_revision=first["catalog_revision"]
    )
    assert len(second["markers"]) == 1 and not second["has_more"]
    assert {item["id"] for item in first["markers"] + second["markers"]} == {
        item["id"] for item in saved
    }
    mutate(client, t, put(client, t), expected=409)
    mutate(client, t, archive(client, t, saved[0]))
    catalog(
        client,
        t,
        after=first["next_after"],
        expected_revision=first["catalog_revision"],
        expected=409,
    )
    # Archiving retains identity/history and does not silently free/reuse an ID.
    mutate(client, t, put(client, t), expected=409)
    assert len(catalog(client, t, all_views=True, include_archived=True)["markers"]) == 2


def test_base_revocation_hides_marker_metadata_threads_and_receipts(client, db_session, markers):
    t = markers
    saved = mutate(client, t, put(client, t))["marker"]
    original = thread(client, t)
    accepted = send(client, t.w, original)
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
    catalog(client, t, expected=404)
    response = client.get(
        marker_path(t) + "/" + saved["id"], headers=headers(t.w.tokens["reviewer"])
    )
    assert response.status_code == 404
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)
    send(client, t.w, original, expected=404)


def test_marker_commit_failure_leaves_no_partial_definition_placement_or_receipt(
    client, db_session, markers
):
    t = markers
    payload = TimelineMarkerCommand.model_validate(put(client, t))
    # HTTP fixtures use nested savepoints on this connection. Finish the setup
    # session's read savepoint so injected rollback cannot rewind their setup.
    db_session.commit()
    with patch.object(db_session, "commit", side_effect=RuntimeError("synthetic commit failure")):
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            service.apply_command(
                db_session, t.w.tokens["owner"], t.w.share.id, uuid.UUID(t.w.entry.id), payload
            )
    for model in (
        m.BaseTimelineMarkerCatalog,
        m.BaseTimelineMarker,
        m.BaseTimelineMarkerPlacement,
        m.BaseTimelineMarkerRevision,
        m.BaseTimelineMarkerReceipt,
    ):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0
    result = service.apply_command(
        db_session, t.w.tokens["owner"], t.w.share.id, uuid.UUID(t.w.entry.id), payload
    )
    assert result["marker"]["revision"] == result["catalog_revision"] == 1

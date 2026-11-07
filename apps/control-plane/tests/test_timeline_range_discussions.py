import copy
import json
import uuid
from pathlib import Path

import pytest
from test_base_discussions import anchor, deny_record, get
from test_base_discussions import targets as target_fixture
from test_base_groups import capture, request
from test_base_groups import groups as group_fixture
from test_base_target_definitions import catalog
from test_bases_registry import base as base_fixture
from test_bases_registry import inspect, path, view
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings

world, base, targets, groups = discussion_world, base_fixture, target_fixture, group_fixture
RANGES = json.loads((Path(__file__).parent / "fixtures/timeline-ranges-v1.json").read_text())[
    "valid"
]


def configure(client, t, *, sequence=False, **options):
    defaults = (
        {"orderProp": "note.order"}
        if sequence
        else {"startDateProp": "note.start", "timeZone": "America/Denver"}
    )
    t.base.save(
        [
            view(
                "Table",
                "sabidurian-sequence" if sequence else "sabidurian-timeline",
                **{**defaults, **options},
            ),
            view("Cards", "cards"),
        ]
    )
    return inspect(client, t.w)


@pytest.fixture
def timeline(client, targets, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_ranges_enabled", True)
    configure(client, targets)
    return targets


def axis_context(client, t, *, actor="reviewer", expected=200, **options):
    response = client.post(
        path(t.w) + "/timeline-axis",
        headers=headers(t.w.tokens[actor]),
        json={"view_id": t.captured["view_id"], **options},
    )
    assert response.status_code == expected, response.text
    return response.json()


def command(client, t, kind="calendar", *, lane=None):
    page = axis_context(client, t)
    axis = page["axis"]
    result = create(t.w)
    result["command"]["anchor"] = {
        **anchor(t, "view"),
        "registry_revision": page["registry_revision"],
        "target": {
            "type": "timeline_range",
            "axis_definition_id": axis["id"],
            "axis_definition_revision": axis["revision"],
            "axis": axis["axis"],
            "range": next(
                copy.deepcopy(case["input"]) for case in RANGES if case["input"]["kind"] == kind
            ),
            **({"lane": lane} if lane is not None else {}),
        },
    }
    return result


def reply(t, thread_id):
    return envelope(
        t.w,
        {
            "type": "reply.create",
            "thread_id": thread_id,
            "message_id": str(uuid.uuid4()),
            "body": body("Follow-up"),
        },
    )


def test_axis_is_readable_by_reviewers_and_isolated_from_legacy_catalog(client, timeline):
    t = timeline
    first = axis_context(client, t)
    assert first == axis_context(client, t, actor="reader")
    assert first["axis"]["kind"] == "axis"
    assert first["axis"]["axis"]["start_property"] == {"source": "note", "name": "start"}
    assert catalog(client, t)["targets"] == []
    axis_context(client, t, expected=422, axis={"renderer": "forged"})
    axis_context(client, t, expected=409, expected_revision=first["registry_revision"] + 1)
    axis_context(client, t, expected=409, view_id=t.snapshot["views"][1]["id"])
    caps = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reader"]),
    ).json()
    assert caps["features"]["discussion_timeline_ranges"] is True
    assert "timeline_range" in caps["discussion_base_targets"]


@pytest.mark.parametrize("kind", ["calendar", "instant", "historical", "ordinal"])
def test_ranges_persist_with_exact_coordinates_and_idempotent_delivery(
    client, db_session, timeline, kind
):
    t = timeline
    if kind == "ordinal":
        configure(client, t, sequence=True)
    original = command(client, t, kind)
    accepted = send(client, t.w, original)
    db_session.expire_all()
    stored = get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
    assert stored["range"] == next(
        case["normalized"] for case in RANGES if case["input"]["kind"] == kind
    )
    assert stored["axis"] == original["command"]["anchor"]["target"]["axis"]
    assert send(client, t.w, original) == accepted
    assert len(get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]) == 1
    assert get(client, t, "/threads", view_id=t.snapshot["views"][1]["id"])["threads"] == []
    send(client, t.w, command(client, t, kind), actor="reader", expected=403)


def test_presentation_changes_preserve_axis_but_semantic_change_and_revert_do_not(client, timeline):
    t = timeline
    original = command(client, t)
    accepted = send(client, t.w, original)
    stale = command(client, t)
    initial = axis_context(client, t)["axis"]
    configure(
        client,
        t,
        startDateProp="start",
        denseMode=True,
        scaleId="decade",
        collapsedGroups=["Done"],
        filters='file.inFolder("Work")',
    )
    assert axis_context(client, t)["axis"] == initial
    send(client, t.w, stale)
    stale = command(client, t)
    configure(client, t, startDateProp="note.other")
    changed = axis_context(client, t)["axis"]
    assert changed["id"] == initial["id"] and changed["revision"] > initial["revision"]
    assert "timeline_axis_changed" in str(send(client, t.w, stale, expected=409))
    configure(client, t)
    restored = axis_context(client, t)["axis"]
    assert restored["fingerprint"] == initial["fingerprint"]
    assert restored["revision"] > changed["revision"]
    send(client, t.w, stale, expected=409)
    assert send(client, t.w, original) == accepted
    send(client, t.w, reply(t, accepted["thread_id"]))


def test_sequence_dense_layout_keeps_property_units_and_axis_switch_rejects_stale_ranges(
    client, timeline
):
    t = timeline
    configure(client, t, sequence=True)
    original = command(client, t, "ordinal")
    configure(client, t, sequence=True, denseMode=True)
    send(client, t.w, original)
    stale = command(client, t, "ordinal")
    configure(client, t)
    send(client, t.w, stale, expected=409)
    forged = command(client, t)
    forged["command"]["anchor"]["target"]["range"] = stale["command"]["anchor"]["target"]["range"]
    send(client, t.w, forged, expected=422)


def test_formula_axis_change_and_revert_fence_capture(client, timeline):
    t = timeline

    def save(expression):
        t.base.save(
            [
                view("Table", "sabidurian-sequence", orderProp="formula.rank"),
                view("Cards", "cards"),
            ],
            formulas={"rank": expression},
        )
        inspect(client, t.w)

    save("rank + 1")
    original = axis_context(client, t)["axis"]
    stale = command(client, t, "ordinal")
    save("rank + 2")
    save("rank + 1")
    current = axis_context(client, t)["axis"]
    assert current["id"] == original["id"] and current["revision"] > original["revision"]
    send(client, t.w, stale, expected=409)


def test_missing_or_unsupported_axis_never_falls_back_to_view_target(client, timeline):
    t = timeline
    for options in (
        {"startDateProp": None},
        {"timeZone": "Unknown/Example"},
        {"startDateProp": "formula.missing"},
    ):
        configure(client, t, **options)
        axis_context(client, t, expected=409)


def test_disabled_creation_retains_threads_and_server_does_not_accept_forged_axis(
    client, timeline, monkeypatch
):
    t = timeline
    original = command(client, t)
    accepted = send(client, t.w, original)
    fresh = command(client, t)
    forged = command(client, t)
    forged["command"]["anchor"]["target"]["axis"]["start_property"]["name"] = "secret"
    send(client, t.w, forged, expected=422)
    forged = command(client, t)
    forged["command"]["anchor"]["target"]["axis_definition_id"] = str(uuid.uuid4())
    send(client, t.w, forged, expected=409)
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_ranges_enabled", False)
    axis_context(client, t, expected=409)
    send(client, t.w, fresh, expected=409)
    assert send(client, t.w, original) == accepted
    send(client, t.w, reply(t, accepted["thread_id"]))


def test_device_zone_is_explicit_when_view_uses_local_zone(client, timeline):
    t = timeline
    configure(client, t, timeZone=None)
    payload = command(client, t)
    payload["command"]["anchor"]["target"]["range"]["time_zone"] = "Future/Example"
    assert "unsupported_timeline_time_zone" in str(send(client, t.w, payload, expected=422))
    payload["command"]["anchor"]["target"]["range"]["time_zone"] = "America/Denver"
    send(client, t.w, payload)


def test_lane_ranges_recheck_exact_source_permissions_on_every_access(
    client, db_session, groups, monkeypatch
):
    t = groups
    monkeypatch.setattr(get_settings(), "collaboration_bases_timeline_ranges_enabled", True)
    configure(client, t, groupBy={"property": "note.status"})
    lane = capture(client, t, request(client, t))["anchor"]["target"]
    original = command(client, t, lane=lane)
    accepted = send(client, t.w, original)
    other = command(client, t)
    global_thread = send(client, t.w, other)
    deny_record(db_session, t)
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)
    send(client, t.w, original, expected=404)
    send(client, t.w, reply(t, accepted["thread_id"]), expected=404)
    send(client, t.w, command(client, t, lane=lane), expected=404)
    assert [
        item["id"] for item in get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]
    ] == [global_thread["thread_id"]]
    get(client, t, f"/threads/{accepted['thread_id']}", actor="owner")


def test_axis_does_not_consume_an_existing_legacy_catalog_slot(client, timeline):
    t = timeline
    t.base.save(
        [view("Table", "sabidurian-sequence", orderProp="note.order"), view("Cards", "cards")],
        formulas={f"value{i}": "1" for i in range(256)},
    )
    inspect(client, t.w)
    assert axis_context(client, t)["axis"]["kind"] == "axis"
    page = catalog(client, t)
    assert len(page["targets"]) == 100 and page["has_more"]
    assert all(item["kind"] == "formula" for item in page["targets"])

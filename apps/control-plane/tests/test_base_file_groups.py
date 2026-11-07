import hashlib
import uuid

import pytest
from test_base_discussions import deny_record, get
from test_base_groups import add_record, base, capture, groups, request, targets, thread, world
from test_base_target_definitions import configure
from test_discussions import headers, send

from app.core.config import get_settings
from app.schemas.bases import CaptureBaseGroup
from app.services import base_file_group_values, base_group_values, base_groups, bases_definition

base, groups, targets, world = base, groups, targets, world


def file_request(client, t, property_name="ext", *, record=None, value=None):
    configure(client, t, groupBy={"property": "file." + property_name})
    record = record or t.record
    payload = request(client, t, record=record, value=value)
    payload.pop("dependency")
    payload.pop("time_zone")
    payload.update(
        source_kind="file", source_path_sha256=hashlib.sha256(record.path.encode()).hexdigest()
    )
    return payload


@pytest.mark.parametrize(
    "path,property_name,value",
    [
        ("Notes/Record.md", "name", "Record"),
        ("Notes/Record.MD", "name", "Record.MD"),
        ("Notes/photo.png", "name", "photo.png"),
        ("Notes/photo.png", "basename", "photo"),
        ("Notes/archive.tar.gz", "basename", "archive.tar"),
        ("Notes/Record.md", "fullname", "Record.md"),
        ("Notes/Record.MD", "ext", "MD"),
        ("README", "ext", ""),
        ("README", "basename", "README"),
        ("Notes/End.", "basename", "End"),
        ("Notes/End.", "name", "End."),
        ("Notes/你好.md", "NAME", "你好"),
        ("Notes/Record.md", "path", "Notes/Record.md"),
        ("Notes/Record.md", "folder", "Notes"),
        ("Record.md", "folder", ""),
    ],
)
def test_inventory_projection_matches_native_file_names_and_share_relative_paths(
    path, property_name, value
):
    identity = base_group_values.RecordIdentity(uuid.uuid4(), uuid.uuid4())
    derived = base_file_group_values.derive(property_name, path, identity)
    assert derived.value == {"type": "text", "value": value}
    assert derived.references == {identity}


@pytest.mark.parametrize(
    "targets", ["markdown", "image", "audio", "video", "pdf", "canvas", "file"], indirect=True
)
def test_file_groups_cover_shared_record_types_without_source_rpc(client, db_session, groups):
    t = groups
    payload = file_request(
        client, t, value={"type": "text", "value": t.record.path.rsplit(".", 1)[-1]}
    )
    deny_record(db_session, t, read=False)  # View comment + source read is sufficient.
    captured = capture(client, t, payload)
    assert t.source.calls == 0
    assert capture(client, t, payload) == captured
    accepted = send(client, t.w, thread(t, captured))
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == captured["anchor"]["target"]
    )
    deny_record(db_session, t)
    capture(client, t, payload, expected=404)
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("name", "Record"),
        ("basename", "Record"),
        ("fullname", "Record.md"),
        ("path", "Notes/Record.md"),
        ("folder", "Notes"),
    ],
)
def test_inventory_groups_compare_authoritative_values_and_refuse_client_mount_prefixes(
    client, groups, property_name, value
):
    t = groups
    payload = file_request(client, t, property_name, value={"type": "text", "value": value})
    captured = capture(client, t, payload)
    assert captured["anchor"]["target"]["value"] == payload["value"]
    assert (
        capture(
            client,
            t,
            {
                **payload,
                "operation_id": str(uuid.uuid4()),
                "value": {"type": "text", "value": "Mount/" + value},
            },
            expected=409,
        )["error"]["message"]["code"]
        == "base_group_value_changed"
    )


def test_file_object_group_is_bound_to_record_lineage_and_remains_accessible_after_rename(
    client, db_session, groups
):
    t = groups
    value = {
        "type": "file",
        "record_id": t.record.id,
        "record_generation": str(t.record.collaboration_generation),
    }
    payload = file_request(client, t, "file", value=value)
    captured = capture(client, t, payload)
    accepted = send(client, t.w, thread(t, captured))
    db_session.refresh(t.record)
    t.record.path = "Notes/Renamed.md"
    db_session.commit()
    assert capture(client, t, payload) == captured  # Exact accepted retry retains evidence.
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == captured["anchor"]["target"]
    )
    assert (
        capture(client, t, {**payload, "operation_id": str(uuid.uuid4())}, expected=409)["error"][
            "message"
        ]["code"]
        == "base_group_source_changed"
    )
    db_session.refresh(t.record)
    t.record.collaboration_generation = uuid.uuid4()
    db_session.commit()
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)


def test_matching_file_group_values_never_authorize_another_threads_protected_source(
    client, db_session, groups
):
    t = groups
    payload = file_request(client, t, value={"type": "text", "value": "md"})
    first = capture(client, t, payload)
    original = send(client, t.w, thread(t, first))
    other = add_record(db_session, t)
    payload = file_request(client, t, record=other, value={"type": "text", "value": "md"})
    second = capture(client, t, payload)
    assert first["anchor"]["target"]["group_id"] == second["anchor"]["target"]["group_id"]
    assert first["anchor"]["target"]["witness_id"] != second["anchor"]["target"]["witness_id"]
    accessible = send(client, t.w, thread(t, second))
    deny_record(db_session, t)
    get(client, t, f"/threads/{original['thread_id']}", expected=404)
    get(client, t, f"/threads/{accessible['thread_id']}")


@pytest.mark.parametrize(
    "property_name",
    ["size", "ctime", "mtime", "tags", "links", "backlinks", "embeds", "properties"],
)
def test_unverified_file_properties_do_not_become_empty_groups(client, groups, property_name):
    t = groups
    payload = file_request(client, t, property_name, value={"type": "empty"})
    assert (
        capture(client, t, payload, expected=409)["error"]["message"]["code"]
        == "base_group_adapter_unavailable"
    )
    assert t.source.calls == 0


def test_capture_shapes_are_disjoint_and_legacy_note_receipt_hashes_are_unchanged(client, groups):
    t = groups
    note = request(client, t)
    expected = bases_definition.fingerprint(
        {"share_id": str(t.w.share.id), "entry_id": str(t.w.entry.id), "request": note}
    )
    assert (
        base_groups._request_hash(t.w.share.id, t.w.entry.id, CaptureBaseGroup.model_validate(note))
        == expected
    )
    capture(client, t, {**note, "source_kind": "file"}, expected=422)
    capture(client, t, {**note, "source_kind": None}, expected=422)
    payload = file_request(client, t, value={"type": "text", "value": "md"})
    for field, value in [
        ("dependency", "AA=="),
        ("dependency", None),
        ("time_zone", "UTC"),
        ("time_zone", None),
    ]:
        capture(client, t, {**payload, field: value}, expected=422)
    capture(client, t, {**payload, "source_path_sha256": "a" * 63}, expected=422)
    capture(client, t, {**payload, "source_kind": None}, expected=422)


def test_file_group_capability_is_explicit_and_disabled_exact_retries_still_authorize(
    client, groups, monkeypatch
):
    t = groups
    url = f"/v1/collaboration/capabilities?share_id={t.w.share.id}"
    response = client.get(url, headers=headers(t.w.tokens["reviewer"]))
    assert response.json()["discussion_base_file_group_properties"] == list(
        base_file_group_values.PROPERTIES
    )
    payload = file_request(client, t, value={"type": "text", "value": "md"})
    captured = capture(client, t, payload)
    monkeypatch.setattr(get_settings(), "collaboration_bases_groups_enabled", False)
    assert (
        client.get(url, headers=headers(t.w.tokens["reviewer"])).json()[
            "discussion_base_file_group_properties"
        ]
        == []
    )
    assert capture(client, t, payload) == captured
    capture(client, t, {**payload, "operation_id": str(uuid.uuid4())}, expected=409)

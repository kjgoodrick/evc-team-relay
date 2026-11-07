"""Parity against native 1.13.7 observations and dependency-preserving derivation."""

import hashlib
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import base_group_values as groups
from app.services import base_source_service as sources

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/native-base-source-1.13.7.json").read_text()
)
SOURCE = groups.RecordIdentity(uuid.UUID(int=1), uuid.UUID(int=2))
DESTINATION = groups.RecordIdentity(uuid.UUID(int=3), uuid.UUID(int=4))
SHARE = uuid.UUID(int=5)
DEFERRED = {"alias", "year_zero", "large_integer"}


def observation(text, source=SOURCE, dependency="AAA="):
    return sources.SourceObservation(
        sources.source_identity(SHARE, source.entry_id, source.generation, dependency),
        "b" * 64,
        hashlib.sha256(text.encode()).hexdigest(),
        True,
        int(time.time() * 1000) + 15000,
        time.monotonic() + 12,
        text,
    )


def derive(
    text,
    property_name="status",
    *,
    resolve=None,
    source=SOURCE,
    path="Team/A.md",
    zone="America/Denver",
):
    return groups.derive_note_value(
        observation(text, source),
        property_name,
        source,
        share_id=SHARE,
        dependency="AAA=",
        source_path=path,
        time_zone=zone,
        resolve_link=resolve or (lambda link, source: DESTINATION if link == "B" else None),
    )


def native_key(value):
    kind = value["type"]
    if kind == "empty":
        return [kind]
    if kind == "list":
        return [kind, [native_key(v) for v in value["value"]]]
    if kind == "link":
        assert value["record_id"] == str(DESTINATION.entry_id)
        assert value["record_generation"] == str(DESTINATION.generation)
        return [kind, "Team/B.md", value["subpath"]]
    if kind == "unresolved_link":
        assert value["record_id"] == str(SOURCE.entry_id)
        assert value["source_path_sha256"] == hashlib.sha256(b"Team/A.md").hexdigest()
        return [kind, value["link"], "Team/A.md"]
    return [kind, value["value"]]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_observed_native_property_types_or_explicit_unavailability(case):
    if case["id"] in DEFERRED or case.get("groupKey") is None:
        with pytest.raises(groups.UnavailableValue):
            derive(case["source"], case["property"])
        return
    result = derive(case["source"], case["property"])
    assert native_key(result.value) == json.loads(case["groupKey"])
    expected = {SOURCE, DESTINATION} if result.value["type"] == "link" else {SOURCE}
    assert result.references == expected
    assert result.source_identity_sha256 == sources.source_identity(
        SHARE, SOURCE.entry_id, SOURCE.generation, "AAA="
    )
    assert result.source_snapshot_sha256 == "b" * 64
    assert result.property_name == case["property"] and result.time_zone == FIXTURE["timeZone"]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_bounded_frontmatter_matches_native_raw_parse_without_changing_base_parser(case):
    if case["id"] in {"alias", "large_integer", "inf", "nan", "duplicate"}:
        with pytest.raises(sources.InvalidMetadata):
            sources.metadata(case["source"], complete=True)
    else:
        assert sources.metadata(case["source"], complete=True) == case["raw"]


def test_nested_links_collect_every_verified_record_and_aliases_do_not_change_identity():
    first = derive('---\nstatus: ["[[B|Alias]]", ["[[B#Section|Caption]]", "[[Absent]]"]]\n---\n')
    second = derive('---\nstatus: ["[[B|Other]]", ["[[B#Section]]", "[[Absent|Renamed]]"]]\n---\n')
    assert first.references == {SOURCE, DESTINATION}
    assert first.fingerprint == second.fingerprint
    assert first.value == second.value
    # A new generation is a new identity, even at the same filename.
    changed = derive(
        '---\nstatus: "[[B]]"\n---\n',
        resolve=lambda *_: replace(DESTINATION, generation=uuid.UUID(int=5)),
    )
    original = derive('---\nstatus: "[[B]]"\n---\n')
    assert changed.fingerprint != original.fingerprint


def test_unresolved_link_identity_preserves_source_and_location_context():
    text = '---\nstatus: "[[Absent]]"\n---\n'
    original = derive(text)
    assert original.fingerprint != derive(text, path="Other/A.md").fingerprint
    assert original.fingerprint != derive(text, source=DESTINATION).fingerprint
    assert original.fingerprint != derive(text, resolve=lambda *_: DESTINATION).fingerprint
    assert original.references == {SOURCE}


def test_authorization_and_ambiguous_resolution_errors_propagate_instead_of_becoming_missing():
    class Denied(Exception):
        pass

    def denied(*_args):
        raise Denied()

    with pytest.raises(Denied):
        derive('---\nstatus: "[[B]]"\n---\n', resolve=denied)
    with pytest.raises(groups.UnavailableValue, match="Unverified"):
        derive('---\nstatus: "[[B]]"\n---\n', resolve=lambda *_: "Team/B.md")


def test_group_identity_excludes_origin_snapshot_but_keeps_types_order_and_date_kind():
    texts = [
        "null",
        '""',
        "false",
        "0",
        '"0"',
        "[]",
        '["1"]',
        "1",
        '"1"',
        '"2026-11-06"',
        '"2026-11-06T00:00:00Z"',
        "[a,b]",
        "[b,a]",
    ]
    values = [derive("---\nstatus: " + value + "\n---\n") for value in texts]
    assert len({v.fingerprint for v in values}) == len(values)
    assert derive("---\nstatus: -0\n---\n").fingerprint == values[3].fingerprint
    original = observation("---\nstatus: Planned\n---\n")
    other = replace(
        observation(original.source_prefix, dependency="AAE="), observed_snapshot_sha256="d" * 64
    )
    args = dict(
        property_name="status",
        source=SOURCE,
        source_path="Team/A.md",
        time_zone="America/Denver",
        resolve_link=lambda *_: None,
        share_id=SHARE,
    )
    a = groups.derive_note_value(original, dependency="AAA=", **args)
    b = groups.derive_note_value(other, dependency="AAE=", **args)
    assert a.fingerprint == b.fingerprint
    assert a.source_snapshot_sha256 != b.source_snapshot_sha256
    # Different records may independently supply the same group's scalar value.
    assert derive(original.source_prefix, source=DESTINATION).fingerprint == a.fingerprint


def test_derivation_cannot_use_another_source_share_dependency_or_expired_observation():
    observed = observation("---\nstatus: Planned\n---\n")
    args = dict(
        property_name="status",
        source=SOURCE,
        share_id=SHARE,
        dependency="AAA=",
        source_path="Team/A.md",
        time_zone="UTC",
        resolve_link=lambda *_: None,
    )
    for changes in (
        {"source": DESTINATION},
        {"share_id": uuid.UUID(int=9)},
        {"dependency": "AAE="},
    ):
        with pytest.raises(HTTPException) as caught:
            groups.derive_note_value(observed, **{**args, **changes})
        assert caught.value.detail == {"code": "anchor_generation_mismatch"}
    with pytest.raises(HTTPException) as caught:
        groups.derive_note_value(replace(observed, deadline=0), **args)
    assert caught.value.detail == {"code": "anchor_validation_expired"}


def test_expiry_during_reference_resolution_prevents_a_derived_result(monkeypatch):
    observed = observation('---\nstatus: "[[B]]"\n---\n')

    def expired(*_args):
        monkeypatch.setattr(sources.anchor_service, "now_ms", lambda: observed.expires_at_ms)
        return DESTINATION

    with pytest.raises(HTTPException) as caught:
        groups.derive_note_value(
            observed,
            "status",
            SOURCE,
            share_id=SHARE,
            dependency="AAA=",
            source_path="Team/A.md",
            time_zone="UTC",
            resolve_link=expired,
        )
    assert caught.value.detail == {"code": "anchor_validation_expired"}


def test_local_dates_use_capture_zone_and_explicit_offsets_ignore_it():
    explicit = "---\nstatus: 2026-11-06T15:30:00-07:00\n---\n"
    assert (
        derive(explicit, zone="UTC").fingerprint == derive(explicit, zone="Asia/Tokyo").fingerprint
    )
    floating = "---\nstatus: 2026-11-06T15:30:00\n---\n"
    assert (
        derive(floating, zone="UTC").fingerprint != derive(floating, zone="Asia/Tokyo").fingerprint
    )
    skipped = "---\nstatus: 2011-12-30\n---\n"
    assert derive(skipped, zone="Pacific/Apia").value == {"type": "date", "value": "2011-12-31"}
    assert derive(skipped, zone="UTC").value == {"type": "date", "value": "2011-12-30"}
    assert derive("---\nstatus: 0001-01-01\n---\n", zone="UTC").value["value"] == "0001-01-01"


@pytest.mark.parametrize(
    "text",
    [
        "2026-00-01",
        "2026-01-00",
        "2026-01-32",
        "2026-02-01T25:00:00Z",
        "2026-02-01T24:01:00Z",
        "2026-02-01T12:60:00Z",
        "2026-02-01T12:00:60Z",
        "2026-02-01T12:00:00+24:00",
        "2026-02-01T12:00:00+01",
        "9999-12-31T24:00:00Z",
    ],
)
def test_invalid_or_unrepresentable_native_dates_do_not_turn_into_text(text):
    with pytest.raises(groups.UnavailableValue):
        derive('---\nstatus: "' + text + '"\n---\n')


@pytest.mark.parametrize(
    "text",
    [
        "http:example.test",
        "http://example.123",
        "http://0x7f000001",
        "http://example.0x",
        "https://ex%61mple.test",
        "http://éxample.test",
        "http://user[me]@example.test",
        "http://[fe80::1%en0]",
    ],
)
def test_unverified_whatwg_forms_remain_explicitly_unavailable(text):
    with pytest.raises(groups.UnavailableValue):
        derive('---\nstatus: "' + text + '"\n---\n')


@pytest.mark.parametrize(
    "text,kind",
    [
        ("https://example.test/a", "url"),
        ("http://127.0.0.1:8080/a", "url"),
        ("http://[::1]/", "url"),
        ("mailto:hello@example.test", "url"),
        ("obsidian://open", "url"),
        ("custom://", "url"),
        ("custom://@", "text"),
        ("custom://:80", "text"),
        ("custom://[bad", "text"),
        ("http://[bad", "text"),
        ("file:///tmp/a", "url"),
        ("file://host:80/a", "text"),
        ("http://host:99999", "text"),
        ("https://example.test/a b", "text"),
    ],
)
def test_url_classification_preserves_original_spelling_and_does_not_render_it(text, kind):
    assert derive('---\nstatus: "' + text + '"\n---\n').value == {"type": kind, "value": text}


def test_resource_limits_apply_after_native_type_interpretation():
    for raw in [
        '"' + "🌿" * 4097 + '"',
        "[" + ",".join("0" for _ in range(256)) + "]",
        "[" * 9 + "0" + "]" * 9,
        "{x: 1}",
    ]:
        with pytest.raises(groups.UnavailableValue):
            derive("---\nstatus: " + raw + "\n---\n")
    assert derive('---\nstatus: "' + "🌿" * 4096 + '"\n---\n').value["type"] == "text"
    # Presentation aliases do not spend the group-identity text budget.
    assert derive('---\nstatus: "[[B|' + "x" * 9000 + ']]"\n---\n').value["type"] == "link"
    for zone in ("", "../UTC", "Unknown/Zone", None):
        with pytest.raises(groups.UnavailableValue):
            derive("---\nstatus: Planned\n---\n", zone=zone)


@pytest.mark.parametrize(
    "scalar", ["!!int 1_000", "!!bool YES", "!!float 1:30", "!!null arbitrary"]
)
def test_explicit_yaml_tags_cannot_bypass_the_native_scalar_grammar(scalar):
    with pytest.raises(sources.InvalidMetadata):
        sources.metadata("---\nstatus: " + scalar + "\n---\n", complete=True)

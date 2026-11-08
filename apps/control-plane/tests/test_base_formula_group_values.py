"""Snapshot binding, bounded shared parsing and reference-preserving formulas."""

from dataclasses import replace

import pytest
from fastapi import HTTPException
from test_base_group_values import DESTINATION, SHARE, SOURCE, observation

from app.services import base_formula_group_values as formulas
from app.services import base_group_values, base_source_service


def derive(source, expression, *, extra=None, resolve=None):
    return formulas.derive(
        source,
        {"group": expression, **(extra or {})},
        "group",
        SOURCE,
        share_id=SHARE,
        dependency="AAA=",
        source_path="Notes/A.md",
        time_zone="UTC",
        resolve_link=resolve or (lambda *_: DESTINATION),
    )


def test_one_metadata_parse_per_verified_snapshot_with_transitive_inputs(monkeypatch):
    original, calls = base_source_service.metadata, []

    def parse(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(base_source_service, "metadata", parse)
    first = observation("---\na: 2\nb: 3\nflag: true\n---\n")
    result = derive(first, 'if(flag, formula.sum + file.name, "other")', extra={"sum": "a + b"})
    assert result.value == {"type": "text", "value": "5A"}
    assert result.references == {SOURCE}
    assert result.source_snapshot_sha256 == first.observed_snapshot_sha256
    assert len(calls) == 1
    second = observation("---\na: 7\nb: 1\nflag: true\n---\n")
    assert derive(second, "a + b").value == {"type": "number", "value": 8}
    assert len(calls) == 2


def test_link_identity_and_all_consulted_references_survive_pass_through():
    result = derive(
        observation('---\nflag: true\nlink: "[[B#Heading]]"\n---\n'), "if(flag, link, absent)"
    )
    assert result.value == {"type": "link", **DESTINATION.wire(), "subpath": "#Heading"}
    assert result.references == {SOURCE, DESTINATION}


def test_unreadable_link_is_not_a_missing_value_or_fallback():
    def denied(*args):
        raise HTTPException(404, "target_unavailable")

    with pytest.raises(HTTPException) as error:
        derive(observation('---\nlink: "[[B]]"\n---\n'), "link", resolve=denied)
    assert error.value.status_code == 404


def test_empty_or_constant_result_still_depends_on_source_and_expiry():
    source = observation("---\nflag: false\n---\n")
    result = derive(source, "if(flag, 1)")
    assert result.value == {"type": "empty"} and result.references == {SOURCE}
    for invalid in [replace(source, identity_sha256="0" * 64), replace(source, expires_at_ms=0)]:
        with pytest.raises(HTTPException):
            derive(invalid, "42")


@pytest.mark.parametrize(
    "expression",
    ["file.path", "file.folder", "file.mtime", "now()", 'if(true, 1, file("Private").secret)'],
)
def test_unverifiable_dependencies_fail_before_value_derivation(expression):
    with pytest.raises(base_group_values.UnavailableValue):
        derive(observation(""), expression)

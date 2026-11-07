import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.timeline_ranges import (
    TimelineRangeValue,
    normalize_timeline_range,
    timeline_range_key,
    validate_timeline_time_zone,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures/timeline-ranges-v1.json").read_text())
ADAPTER = TypeAdapter(TimelineRangeValue)


@pytest.mark.parametrize("case", FIXTURES["valid"], ids=lambda case: case["name"])
def test_valid_coordinates_and_wire_identity(case):
    original = json.dumps(case["input"])
    actual = normalize_timeline_range(case["input"])
    assert actual == case["normalized"]
    assert timeline_range_key(actual) == json.dumps(case["key"], separators=(",", ":"))
    assert ADAPTER.validate_json(original) == actual
    assert ADAPTER.validate_json(ADAPTER.dump_json(actual)) == actual
    assert json.dumps(case["input"]) == original


@pytest.mark.parametrize("case", FIXTURES["invalid"], ids=lambda case: case["name"])
def test_invalid_coordinates_are_refused(case):
    with pytest.raises(ValueError):
        normalize_timeline_range(case["input"])
    with pytest.raises(ValidationError):
        ADAPTER.validate_json(json.dumps(case["input"]))


def test_capture_requires_local_zone_support_without_rewriting_aliases():
    assert validate_timeline_time_zone("America/Denver") == "America/Denver"
    assert validate_timeline_time_zone("UTC") == "UTC"
    with pytest.raises(ValueError, match="Unsupported timeline time zone"):
        validate_timeline_time_zone("Future/Example")
    with pytest.raises(ValueError):
        validate_timeline_time_zone("../UTC")


@pytest.mark.parametrize("version", [float("nan"), float("inf"), -float("inf"), 10**400])
def test_python_numeric_edge_cases_return_validation_errors(version):
    with pytest.raises(ValueError):
        normalize_timeline_range({**FIXTURES["valid"][0]["input"], "version": version})


def test_integral_json_numbers_match_javascript():
    value = {**FIXTURES["valid"][0]["input"], "version": 1.0}
    assert normalize_timeline_range(value)["version"] == 1

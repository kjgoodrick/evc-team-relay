"""Calculations are checked against captured native results, not a second model."""

import json
from pathlib import Path

import pytest
from test_base_group_values import SOURCE, derive

from app.services import base_file_group_values
from app.services.base_formula_expression import UnsupportedFormula, compile_formula
from app.services.base_formula_values import evaluate

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/native-base-formula-1.13.7.json").read_text()
)
FORMULAS = {case["id"]: case["expression"] for case in FIXTURE["cases"]}
DEFERRED = {"power", "ifLazy", "notList", "listTruthy"}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_native_primitive_calculations_or_explicit_unavailability(case):
    def source(kind, name):
        if kind == "file":
            return base_file_group_values.derive(name, "Source.md", SOURCE).value
        assert kind == "note"
        return derive(FIXTURE["source"], name).value

    if case["id"] in DEFERRED or any(key in case for key in ("error", "nonfinite", "deferred")):
        with pytest.raises(UnsupportedFormula):
            evaluate(compile_formula(FORMULAS, case["id"]), source)
    else:
        assert evaluate(compile_formula(FORMULAS, case["id"]), source) == case["value"]


def test_all_references_used_for_branch_choice_are_retained_and_memoized():
    inputs = []

    def source(kind, name):
        inputs.append((kind, name))
        return {"type": "number", "value": 2.0}

    program = compile_formula(
        {"root": "if(flag, formula.shared + formula.shared, unused)", "shared": "amount * 2"},
        "root",
    )
    assert evaluate(program, source) == {"type": "number", "value": 8.0}
    assert inputs == [("note", "flag"), ("note", "amount")]
    assert program.properties == {("note", "flag"), ("note", "amount"), ("note", "unused")}


def test_source_denial_never_becomes_null_false_or_an_alternative_branch():
    class Denied(Exception):
        pass

    def denied(*args):
        raise Denied()

    for expr in ['if(secret, "yes", "no")', "!secret", "secret == null", "secret || true"]:
        with pytest.raises(Denied):
            evaluate(compile_formula({"root": expr}, "root"), denied)


def test_output_bounds_overflow_and_unverified_type_conversion():
    inputs = {
        "long": {"type": "text", "value": "x" * 5000},
        "big": {"type": "number", "value": 1e308},
    }
    for expr in ["long + long", "big * big", "big / 0", '"value " + 2.3', '"x" == 2']:
        with pytest.raises(UnsupportedFormula):
            evaluate(compile_formula({"root": expr}, "root"), lambda kind, name: inputs[name])


def test_pass_through_preserves_link_identity_without_mutating_the_source():
    value = {
        "type": "link",
        "record_id": "source-id",
        "record_generation": "source-generation",
        "subpath": "#section",
    }
    result = evaluate(
        compile_formula({"root": "if(true, destination, missing)"}, "root"), lambda *_: value
    )
    assert result == value
    assert value == {
        "type": "link",
        "record_id": "source-id",
        "record_generation": "source-generation",
        "subpath": "#section",
    }

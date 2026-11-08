import pytest

from app.services import base_presence_formulas as formulas


def plan(source, **other):
    return formulas.plan(
        {"formulas": {"bucket": source, **other}}, {"groupBy": {"property": "formula.bucket"}}
    )


def test_transitive_plan_includes_all_static_inputs_without_evaluating_branches():
    assert plan('if(status == "Ready", formula.double, hidden)', double="order * 2") == {
        "property": {"source": "formula", "name": "bucket"},
        "inputs": [
            {"source": "note", "name": "hidden"},
            {"source": "note", "name": "order"},
            {"source": "note", "name": "status"},
        ],
    }
    assert plan('"constant"')["inputs"] == []
    assert plan("file.basename")["inputs"] == [{"source": "file", "name": "basename"}]


@pytest.mark.parametrize(
    "source,other",
    [
        ('if(true, 1, file("Private").score)', {}),
        ("file.path", {}),
        ("formula.loop", {"loop": "formula.bucket"}),
        ("formula.missing", {}),
        (" + ".join(f"p{i}" for i in range(33)), {}),
        ('note["' + "x" * 129 + '"]', {}),
        ("1", {"unrelated": "x" * 65_536}),
    ],
)
def test_unbounded_or_unsupported_closures_have_no_plan(source, other):
    assert plan(source, **other) is None


def test_cached_plans_are_bounded_and_callers_cannot_mutate_them():
    formulas._inputs.cache_clear()
    first = plan("status")
    first["inputs"][0]["name"] = "corrupt"
    assert plan("status")["inputs"][0]["name"] == "status"
    assert formulas._inputs.cache_info().hits == 1
    for n in range(40):
        assert plan(str(n))
    assert formulas._inputs.cache_info().currsize == 32

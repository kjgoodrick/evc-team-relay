"""Allowlisted Bases syntax must never infer authority from a partial parse."""

import pytest

from app.services.base_formula_expression import UnsupportedFormula, compile_formula, parse


def test_precedence_static_properties_and_transitive_dependency_closure():
    program = compile_formula(
        {
            "group": 'if(note.status == "Ready", formula.double, note["fallback value"])',
            "double": "(order + 1) * 2",
            "unused": 'file("Private.md").password',
        },
        "group",
    )
    assert program.properties == {("note", "status"), ("note", "order"), ("note", "fallback value")}
    assert [name for name, _ in program.expressions] == ["double", "group"]
    tree = parse("1 + 2 * 3 >= 4 && !false")
    assert tree.kind == "binary" and tree.value == "&&"
    assert tree.args[0].value == ">="
    assert tree.args[0].args[0].value == "+"
    assert tree.args[0].args[0].args[1].value == "*"


def test_strings_escaping_and_methods_do_not_become_dependencies():
    program = compile_formula(
        {"group": """if(status.lower().startsWith('r'), "file('Secret').password", note['a.b'])"""},
        "group",
    )
    assert program.properties == {("note", "status"), ("note", "a.b")}
    assert parse("'a\\'b\\nc'").value == "a'b\nc"
    assert parse('"a\\\\b"').value == "a\\b"
    assert parse("-.5").args[0].value == 0.5


@pytest.mark.parametrize(
    "source",
    [
        'file("Secret.md").password',
        "this.secret",
        "note[secret]",
        "formula[which]",
        "note.status.asFile()",
        "note.link.asFile().secret",
        "now()",
        "random()",
        'html("secret")',
        'image("https://example.com")',
        'note.status.replace(/.*/, "")',
        "note.status.constructor",
        "unknown(1)",
        "note.status.unknown()",
        '1; file("Secret")',
        "1 // trailing",
        "1 /* trailing */",
        "1 === 1",
        "true ? 1 : 2",
        'null ?? "x"',
        "file.mtime",
        "file.links",
        "file.size",
        "note.status[0]",
        "value",
        "index",
        "acc",
        "note",
        'note[""]',
        'if(true, 1, file("Secret"))',
        "if(true, 1, formula.missing)",
        "1e309",
        "1e2",
        "+2",
        "1e+",
        '"unterminated',
        '"a\\x62"',
        '"a\nb"',
        "min()",
        "number(1, 2)",
        "if(true)",
        "status.lower(1)",
        "",
    ],
)
def test_unknown_or_ambient_syntax_fails_closed_even_in_unused_branches(source):
    with pytest.raises(UnsupportedFormula):
        compile_formula({"group": source}, "group")


@pytest.mark.parametrize(
    "formulas",
    [
        {"a": "formula.a"},
        {"a": "formula.b", "b": "formula.a"},
        {"a": "formula.absent"},
        {"a": 42},
    ],
)
def test_invalid_formula_graphs_cannot_supply_a_result(formulas):
    with pytest.raises(UnsupportedFormula):
        compile_formula(formulas, "a")


def test_source_nodes_depth_and_graph_work_are_bounded():
    for source in ['"' + "a" * 8192 + '"', "(" * 30 + "1" + ")" * 30, "+".join(["1"] * 300)]:
        with pytest.raises(UnsupportedFormula):
            parse(source)
    formulas = {f"f{i}": f"formula.f{i + 1}" for i in range(30)}
    formulas["f30"] = "1"
    with pytest.raises(UnsupportedFormula):
        compile_formula(formulas, "f0")
    formulas = {f"f{i}": "+".join(["1"] * 100) for i in range(16)}
    formulas["root"] = "+".join(f"formula.f{i}" for i in range(16))
    with pytest.raises(UnsupportedFormula):
        compile_formula(formulas, "root")


def test_repeated_references_share_one_compiled_expression():
    program = compile_formula(
        {"root": "formula.a + formula.a + formula.a", "a": "file.name"}, "root"
    )
    assert len(program.expressions) == 2
    assert program.properties == {("file", "name")}

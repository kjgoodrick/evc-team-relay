"""Derive formula groups from one verified note and current managed inventory.

No client expression, source body, dependency list or computed result grants
authority. Compile the synchronized Base's formula closure and retain every
reference actually consulted, including inputs used only to select a branch.
"""

from app.services import (
    base_file_group_values,
    base_formula_expression,
    base_formula_values,
    base_group_values,
    bases_definition,
)


def derive(
    observation,
    formulas,
    name,
    source,
    *,
    share_id,
    dependency,
    source_path,
    time_zone,
    resolve_link,
):
    source.wire()
    observation.check(share_id, source.entry_id, source.generation, dependency)
    references = {source}
    try:
        program = base_formula_expression.compile_formula(formulas, name)

        def property_value(kind, property_name):
            if kind == "file":
                derived = base_file_group_values.derive(property_name, source_path, source)
            else:
                derived = base_group_values.derive_note_value(
                    observation,
                    property_name,
                    source,
                    share_id=share_id,
                    dependency=dependency,
                    source_path=source_path,
                    time_zone=time_zone,
                    resolve_link=resolve_link,
                )
            references.update(derived.references)
            if len(references) > 256:
                raise base_group_values.UnavailableValue("Formula references exceed limit")
            return derived.value

        value = base_formula_values.evaluate(program, property_value)
    except base_formula_expression.UnsupportedFormula:
        raise base_group_values.UnavailableValue("Unsupported formula calculation") from None
    observation.check(share_id, source.entry_id, source.generation, dependency)
    return base_group_values.DerivedValue(
        value,
        bases_definition.fingerprint({"version": 1, "value": value}),
        frozenset(references),
        observation.identity_sha256,
        observation.observed_snapshot_sha256,
        name,
        time_zone,
    )

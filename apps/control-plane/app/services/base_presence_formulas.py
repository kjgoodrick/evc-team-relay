"""Bounded syntax plans for read-only formula-group presence.

No record source, formula result, or permission decision is cached. Clients read
every static input from the selected native member (including inactive branches)
and include its linked records before publishing or matching a fingerprint.
"""

import json
from functools import lru_cache

from app.core.config import get_settings
from app.services import base_formula_expression, base_target_definitions, bases_registry


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_presence_enabled
        and settings.collaboration_presence_formula_groups_enabled
        and bases_registry.enabled()
    )


def _name(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
        and len(value.encode("utf-8")) <= 256
    )


@lru_cache(maxsize=32)
def _inputs(serialized, name):
    try:
        program = base_formula_expression.compile_formula(json.loads(serialized), name)
        properties = tuple(sorted(program.properties))
        if len(properties) > 32:
            return None
        for _, key in properties:
            if not _name(key):
                return None
        return properties
    except (ValueError, TypeError):
        return None


def plan(configuration, descriptor):
    grouping = descriptor.get("groupBy") if isinstance(descriptor, dict) else None
    if not isinstance(grouping, dict) or set(grouping) - {"property", "direction"}:
        return None
    prop = base_target_definitions.property_locator(grouping.get("property"))
    if (
        not prop
        or prop["source"] != "formula"
        or not _name(prop["name"])
        or not isinstance(configuration, dict)
    ):
        return None
    try:
        serialized = json.dumps(
            configuration.get("formulas", {}), sort_keys=True, ensure_ascii=True
        )
        if len(serialized) > 65_536:
            return None
        inputs = _inputs(serialized, prop["name"])
    except (ValueError, TypeError, RecursionError):
        return None
    if inputs is None:
        return None
    return {
        "property": prop,
        "inputs": [{"source": source, "name": name} for source, name in inputs],
    }

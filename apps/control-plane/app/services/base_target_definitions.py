"""Identity of definitions, never authority over their computed values.

Only verified server-held configuration is inspected. No formula, filter, plugin
code or aggregate is evaluated. Result membership and cross-file dependencies
must be verified separately before computed values may be quoted or published.
"""

from dataclasses import dataclass

from sqlalchemy import select, tuple_

from app.db import models as m
from app.services import bases_definition as codec

MAX_PER_VIEW = 256
MAX_PER_DEFINITION = 1024
PAGE_SIZE = 100


class TargetLimit(ValueError):
    pass


def name(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def property_locator(value):
    if not isinstance(value, str):
        return None
    for source in ("note", "file", "formula"):
        if value.startswith(source + "."):
            key = value[len(source) + 1 :]
            return {"source": source, "name": key} if name(key) else None
    return {"source": "note", "name": value} if name(value) else None


@dataclass(frozen=True)
class Candidate:
    kind: str
    name: str
    descriptor: dict
    fingerprint: str


def candidates(configuration, view):
    """Ignore presentation, retain semantic inputs and bound work before SQL.

    Until a validated expression AST can prove a narrower dependency closure,
    any formula change invalidates formula-dependent identities. This also covers
    transitive/dynamic references and custom functions without guessing at regex
    dependencies. No evaluated value or client dependency claim is accepted.
    """
    formulas = configuration.get("formulas", {})
    summaries = configuration.get("summaries", {})
    formulas = formulas if isinstance(formulas, dict) else {}
    summaries = summaries if isinstance(summaries, dict) else {}
    formula_hash = codec.fingerprint(formulas)
    summary_hash = codec.fingerprint(summaries)
    valid_formulas = {
        key
        for key, expression in formulas.items()
        if name(key) and isinstance(expression, str) and expression.strip()
    }
    output = []

    def add(kind, key, descriptor, semantic):
        output.append(Candidate(kind, key, descriptor, codec.fingerprint(semantic)))
        if len(output) > MAX_PER_VIEW:
            raise TargetLimit("Too many target definitions")

    for key in formulas:
        if key in valid_formulas:
            add(
                "formula",
                key,
                {"property": {"source": "formula", "name": key}},
                {"property": key, "formulas": formula_hash},
            )
    group = view.get("groupBy")
    locator = property_locator(group.get("property")) if isinstance(group, dict) else None
    if locator and (locator["source"] != "formula" or locator["name"] in valid_formulas):
        # Direction, group ordering, membership and filters do not change what
        # the group means. Unknown future grouping options do: preserve them.
        semantic = {k: v for k, v in group.items() if k not in {"property", "direction"}}
        semantic["property"] = locator
        if locator["source"] == "formula":
            semantic["formulas"] = formula_hash
        add("group", "groupBy", {"property": locator}, semantic)
    assigned = view.get("summaries", {})
    if isinstance(assigned, dict):
        # Aliases such as status and note.status must not create two identities
        # for one property. Omit ambiguous assignments rather than choosing one.
        normalized = {}
        for key, summary in assigned.items():
            prop = property_locator(key)
            if prop and name(summary):
                identity = (prop["source"], prop["name"])
                normalized.setdefault(identity, []).append((prop, summary))
        for (source, key), choices in normalized.items():
            if len(choices) != 1:
                continue
            prop, summary = choices[0]
            if source == "formula" and key not in valid_formulas:
                continue
            if summary in summaries and (
                not isinstance(summaries[summary], str) or not summaries[summary].strip()
            ):
                continue
            # A custom summary with the same name overrides a built-in one.
            # Keep every custom definition in the revision closure, since
            # plugin-defined functions may have dependencies we cannot inspect.
            semantic = {
                "property": prop,
                "summary": summary,
                "custom_summaries": summary_hash,
                "formulas": formula_hash,
                "filters": [configuration.get("filters"), view.get("filters")],
                "limit": view.get("limit"),
            }
            if view.get("limit") is not None:
                semantic["sort"] = view.get("sort")
            # Keep the whole valid property name; hashing its typed locator also
            # bounds the SQL key for long names without prefix collisions.
            add(
                "summary", codec.fingerprint(prop), {"property": prop, "summary": summary}, semantic
            )
    from app.services.timeline_axes import candidate as axis_candidate

    axis = axis_candidate(configuration, view)
    if axis:
        # One separately bounded axis per saved view must not consume a legacy
        # formula/group/summary slot or disable an existing full catalog.
        descriptor, semantic = axis
        output.append(Candidate("axis", "primary", descriptor, codec.fingerprint(semantic)))
    return output


def compile_definition(configuration, views):
    result, count = {}, 0
    for view in views:
        if view.state != "active":
            continue
        items = candidates(configuration, view.descriptor)
        count += sum(item.kind != "axis" for item in items)
        if count > MAX_PER_DEFINITION:
            raise TargetLimit("Too many target definitions")
        result[view.id] = items
    return result


def synchronize(db, definition):
    """Caller owns the share lock; commit together with the view registry.

    The table is derived lazily for pre-migration registries, and refreshed on
    every registry transition. Limit failure disables semantic capture without
    rejecting an otherwise supported .base file or its existing discussions.
    """
    views = list(
        db.scalars(
            select(m.BaseView).where(
                m.BaseView.definition_id == definition.id, m.BaseView.state == "active"
            )
        )
    )
    # Archived views can accumulate without bound. Only current views contribute
    # candidates; existing active rows are fetched independently below.
    try:
        compiled = compile_definition(definition.configuration, views)
        available = True
    except TargetLimit:
        compiled, available = {}, False
    keys = [
        (view_id, item.kind, item.name) for view_id, items in compiled.items() for item in items
    ]
    rows = list(
        db.scalars(
            select(m.BaseTargetDefinition)
            .join(m.BaseView)
            .where(
                m.BaseView.definition_id == definition.id, m.BaseTargetDefinition.active.is_(True)
            )
        )
    )
    if keys:
        # Include inactive keys that reappear; never scan the full history.
        rows.extend(
            db.scalars(
                select(m.BaseTargetDefinition).where(
                    m.BaseTargetDefinition.active.is_(False),
                    tuple_(
                        m.BaseTargetDefinition.view_id,
                        m.BaseTargetDefinition.kind,
                        m.BaseTargetDefinition.name,
                    ).in_(keys),
                )
            )
        )
    known = {(row.view_id, row.kind, row.name): row for row in rows}
    desired = set(keys)
    for key, row in known.items():
        if key not in desired and row.active:
            row.active, row.revision = False, definition.revision
            row.observed_revision = definition.revision
    for view_id, items in compiled.items():
        for item in items:
            row = known.get((view_id, item.kind, item.name))
            if row is None:
                db.add(
                    m.BaseTargetDefinition(
                        view_id=view_id,
                        kind=item.kind,
                        name=item.name,
                        revision=definition.revision,
                        observed_revision=definition.revision,
                        fingerprint=item.fingerprint,
                        active=True,
                        descriptor=item.descriptor,
                    )
                )
            else:
                # A rollback to an older writer may skip semantic bookkeeping.
                # Do not reuse old proof across an unobserved change-and-revert.
                if (
                    not row.active
                    or row.fingerprint != item.fingerprint
                    or definition.revision > row.observed_revision + 1
                ):
                    row.active, row.revision = True, definition.revision
                    row.fingerprint, row.descriptor = item.fingerprint, item.descriptor
                row.observed_revision = definition.revision
    db.flush()
    return available


def public(row):
    return dict(
        id=str(row.id),
        kind=row.kind,
        revision=row.revision,
        fingerprint=row.fingerprint,
        **row.descriptor,
    )


def catalog(db, token, share_id, entry_id, payload):
    from app.services import base_discussions, bases_registry, discussion_service

    discussion_service.require_clean_session(db)
    try:
        if not base_discussions.enabled():
            base_discussions.error(409, "base_discussions_disabled")
        ctx = bases_registry.context(db, token, share_id, entry_id, write=True)
        definition, view = base_discussions.view_for(db, ctx, payload.view_id, active=True)
        if definition.content_sha256 != ctx.entry.record.get("hash"):
            base_discussions.error(409, "base_definition_not_synced")
        if (
            payload.expected_revision is not None
            and payload.expected_revision != definition.revision
        ):
            base_discussions.error(409, "stale_base_revision")
        if not synchronize(db, definition):
            base_discussions.error(409, "base_definition_targets_limit")
        query = select(m.BaseTargetDefinition).where(
            m.BaseTargetDefinition.view_id == view.id,
            m.BaseTargetDefinition.active.is_(True),
            # Old clients strictly decode this catalog. Axis discovery has a
            # separate versioned response and must not alter their wire shape.
            m.BaseTargetDefinition.kind.in_(("formula", "group", "summary")),
        )
        if payload.after:
            query = query.where(m.BaseTargetDefinition.id > payload.after)
        rows = list(db.scalars(query.order_by(m.BaseTargetDefinition.id).limit(PAGE_SIZE + 1)))
        page = rows[:PAGE_SIZE]
        result = dict(
            protocol_version=1,
            entry_id=ctx.entry.id,
            definition_id=str(definition.id),
            view_id=str(view.id),
            generation=str(definition.generation),
            registry_revision=definition.revision,
            targets=[public(row) for row in page],
            has_more=len(rows) > PAGE_SIZE,
            next_after=str(page[-1].id) if page else None,
            computed_values=False,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise

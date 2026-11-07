"""Verified saved-view axis semantics, independent of viewport and record values."""

from sqlalchemy import select

from app.db import models as m
from app.schemas.timeline_ranges import validate_timeline_time_zone


def candidate(configuration, view):
    from app.services.base_target_definitions import property_locator
    from app.services.bases_definition import fingerprint

    renderer = view.get("type")
    if renderer not in ("sabidurian-timeline", "sabidurian-sequence"):
        return None
    sequence = renderer == "sabidurian-sequence"
    start = property_locator(view.get("orderProp" if sequence else "startDateProp"))
    end_value = view.get("orderEndProp" if sequence else "endDateProp")
    end = property_locator(end_value) if end_value not in (None, "") else None
    if not start or end_value not in (None, "") and not end:
        return None
    zone = None if sequence else view.get("timeZone") or None
    if zone is not None:
        try:
            validate_timeline_time_zone(zone)
        except ValueError:
            return None
    axis = dict(
        version=1, renderer=renderer, start_property=start, end_property=end, time_zone=zone
    )
    semantic = dict(axis=axis)
    if any(prop and prop["source"] == "formula" for prop in (start, end)):
        formulas = configuration.get("formulas", {})
        if not isinstance(formulas, dict) or any(
            prop and prop["source"] == "formula" and not isinstance(formulas.get(prop["name"]), str)
            for prop in (start, end)
        ):
            return None
        # Formula dependency closure is conservative; no expression is executed.
        semantic["formulas"] = fingerprint(formulas)
    return {"axis": axis}, semantic


def context(db, token, share_id, entry_id, payload):
    from app.services import (
        base_discussions,
        base_target_definitions,
        bases_registry,
        discussion_service,
    )

    discussion_service.require_clean_session(db)
    try:
        if not base_discussions.timeline_ranges_enabled():
            base_discussions.error(409, "timeline_ranges_disabled")
        ctx = bases_registry.context(db, token, share_id, entry_id, write=True)
        definition, view = base_discussions.view_for(db, ctx, payload.view_id, active=True)
        if definition.content_sha256 != ctx.entry.record.get("hash"):
            base_discussions.error(409, "base_definition_not_synced")
        if (
            payload.expected_revision is not None
            and payload.expected_revision != definition.revision
        ):
            base_discussions.error(409, "stale_base_revision")
        if not base_target_definitions.synchronize(db, definition):
            base_discussions.error(409, "base_definition_targets_limit")
        axis = db.scalar(
            select(m.BaseTargetDefinition).where(
                m.BaseTargetDefinition.view_id == view.id,
                m.BaseTargetDefinition.kind == "axis",
                m.BaseTargetDefinition.active.is_(True),
            )
        )
        if axis is None:
            base_discussions.error(409, "timeline_axis_unavailable")
        result = dict(
            protocol_version=1,
            entry_id=ctx.entry.id,
            definition_id=str(definition.id),
            view_id=str(view.id),
            generation=str(definition.generation),
            registry_revision=definition.revision,
            axis=base_target_definitions.public(axis),
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def validate(db, ctx, definition, anchor, *, creation, write, user):
    from app.services import base_discussions, base_target_definitions

    target = anchor.target
    if creation:
        if not base_discussions.timeline_ranges_enabled():
            base_discussions.error(409, "timeline_ranges_disabled")
        if not base_target_definitions.synchronize(db, definition):
            base_discussions.error(409, "base_definition_targets_limit")
    axis = db.get(m.BaseTargetDefinition, target.axis_definition_id)
    if not axis or axis.kind != "axis" or axis.view_id != anchor.view_id:
        base_discussions.error(
            409 if creation else 404,
            "timeline_axis_unavailable" if creation else "target_unavailable",
        )
    if creation:
        if (
            not axis.active
            or axis.revision != target.axis_definition_revision
            or axis.revision > anchor.registry_revision
        ):
            base_discussions.error(409, "timeline_axis_changed")
        if axis.descriptor.get("axis") != target.axis.model_dump(mode="json"):
            base_discussions.error(422, "invalid_anchor")
        zone = target.range.get("time_zone")
        if zone is not None:
            try:
                validate_timeline_time_zone(zone)
            except ValueError:
                base_discussions.error(422, "unsupported_timeline_time_zone")
    if target.lane is not None:
        # Preserve the exact group source witness and its view, lineage, current
        # read authority and creation-time definition checks. Never use a label.
        base_discussions.validate(
            db,
            ctx,
            anchor.model_copy(update={"target": target.lane}),
            creation=creation,
            write=write,
            user=user,
        )

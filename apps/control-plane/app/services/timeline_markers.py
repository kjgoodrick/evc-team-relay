"""Shared marker identity, editor-only CAS and immutable captured context.

Definitions belong to one managed .base file; placements are explicit saved-view
associations. Nothing reads or writes a client's local plugin settings. All
transactions use the same authenticated share lock as discussions/inventory.
"""

import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as ds
from app.schemas.timeline_markers import PutTimelineMarker
from app.schemas.timeline_ranges import validate_timeline_time_zone
from app.services import (
    base_discussions,
    base_target_definitions,
    bases_registry,
    collaboration_restore,
)
from app.services import discussion_service as discussions

MAX_MARKERS = 256
MAX_RETAINED_PLACEMENTS = 256
PAGE_SIZE = 50
error = discussions.error


def enabled():
    return (
        base_discussions.timeline_ranges_enabled()
        and get_settings().collaboration_bases_timeline_markers_enabled
    )


def status_enabled():
    return bases_registry.enabled() and get_settings().collaboration_discussions_enabled


def scope(db, token, share_id, entry_id, *, write=False):
    ctx = bases_registry.context(db, token, share_id, entry_id, write=write, edit=write)
    definition = base_discussions.definition_for(db, ctx)
    return ctx, definition


def state(db, definition):
    catalog = db.get(m.BaseTimelineMarkerCatalog, definition.id)
    return catalog, catalog.revision if catalog else 0


def identity(definition, revision):
    return dict(
        protocol_version=1,
        definition_id=str(definition.id),
        entry_id=definition.entry_id,
        generation=str(definition.generation),
        registry_revision=definition.revision,
        catalog_revision=revision,
    )


def public(marker):
    return dict(
        id=str(marker.id),
        revision=marker.revision,
        active=marker.active,
        content=marker.content,
    )


def placement_value(placement):
    return dict(
        view_id=str(placement.view_id),
        active=placement.active,
        axis_definition_id=str(placement.axis_definition_id),
        axis_definition_revision=placement.axis_definition_revision,
        axis=placement.axis,
    )


def placements(db, marker):
    return list(
        db.scalars(
            select(m.BaseTimelineMarkerPlacement)
            .where(m.BaseTimelineMarkerPlacement.marker_id == marker.id)
            .order_by(m.BaseTimelineMarkerPlacement.view_id)
        )
    )


def snapshot(db, marker):
    return {
        **public(marker),
        "placements": [placement_value(item) for item in placements(db, marker)],
    }


def target(marker, placement):
    return dict(
        type="timeline_marker",
        marker_id=str(marker.id),
        marker_revision=marker.revision,
        content=marker.content,
        **{
            key: value
            for key, value in placement_value(placement).items()
            if key not in {"view_id", "active"}
        },
    )


def list_markers(db, token, share_id, entry_id, payload):
    discussions.require_clean_session(db)
    try:
        _ctx, definition = scope(db, token, share_id, entry_id)
        _catalog, revision = state(db, definition)
        if payload.expected_revision is not None and payload.expected_revision != revision:
            error(409, "timeline_marker_catalog_changed")
        query = select(m.BaseTimelineMarker).where(
            m.BaseTimelineMarker.definition_id == definition.id
        )
        if payload.view_id is not None:
            base_discussions.view_for(db, _ctx, payload.view_id)
            query = query.join(m.BaseTimelineMarkerPlacement).where(
                m.BaseTimelineMarkerPlacement.view_id == payload.view_id
            )
            if not payload.include_archived:
                query = query.where(m.BaseTimelineMarkerPlacement.active.is_(True))
        if not payload.include_archived:
            query = query.where(m.BaseTimelineMarker.active.is_(True))
        if payload.after is not None:
            query = query.where(m.BaseTimelineMarker.id > payload.after)
        rows = list(db.scalars(query.order_by(m.BaseTimelineMarker.id).limit(PAGE_SIZE + 1)))
        view_placements = (
            {
                item.marker_id: item
                for item in db.scalars(
                    select(m.BaseTimelineMarkerPlacement).where(
                        m.BaseTimelineMarkerPlacement.marker_id.in_(
                            [item.id for item in rows[:PAGE_SIZE]]
                        ),
                        m.BaseTimelineMarkerPlacement.view_id == payload.view_id,
                    )
                )
            }
            if payload.view_id is not None and rows
            else {}
        )
        result = []
        for marker in rows[:PAGE_SIZE]:
            value = public(marker)
            if payload.view_id is not None:
                placement = view_placements[marker.id]
                value["placement"] = placement_value(placement)
                value["target"] = target(marker, placement)
            result.append(value)
        response = dict(
            **identity(definition, revision),
            markers=result,
            has_more=len(rows) > PAGE_SIZE,
            next_after=str(rows[min(len(rows), PAGE_SIZE) - 1].id)
            if rows
            else (str(payload.after) if payload.after else None),
        )
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def read_marker(db, token, share_id, entry_id, marker_id):
    discussions.require_clean_session(db)
    try:
        _ctx, definition = scope(db, token, share_id, entry_id)
        marker = db.get(m.BaseTimelineMarker, marker_id)
        if marker is None or marker.definition_id != definition.id:
            error(404, "target_unavailable")
        _catalog, revision = state(db, definition)
        response = {**identity(definition, revision), "marker": snapshot(db, marker)}
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def checked_placement(db, definition, requested, content):
    view = db.get(m.BaseView, requested.view_id)
    if not view or view.definition_id != definition.id or view.state != "active":
        error(409, "base_view_unavailable")
    axis = db.get(m.BaseTargetDefinition, requested.axis_definition_id)
    if not axis or axis.view_id != view.id or axis.kind != "axis" or not axis.active:
        error(409, "timeline_axis_unavailable")
    if axis.revision != requested.axis_definition_revision or axis.revision > definition.revision:
        error(409, "timeline_axis_changed")
    try:
        # Reuse the exact range/axis compatibility contract, including timezone.
        ds.BaseTimelineRangeTarget.model_validate(
            dict(
                type="timeline_range",
                axis_definition_id=axis.id,
                axis_definition_revision=axis.revision,
                axis=axis.descriptor["axis"],
                range=content.range,
            )
        )
        zone = content.range.get("time_zone")
        if zone is not None:
            validate_timeline_time_zone(zone)
    except (ValueError, KeyError, ValidationError):
        error(422, "invalid_marker_placement")
    return axis


def command_hash(payload):
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        error(413, "command_too_large")
    return hashlib.sha256(encoded.encode()).hexdigest()


def command_status(db, token, share_id, entry_id, payload):
    """Read this actor's exact receipt without replay or revision reconciliation.

    Missing receipts remain unknown, including after restore. Current read access
    to the base is required; prior edit authority never grants present access.
    """
    discussions.require_clean_session(db)
    if not status_enabled():
        error(404, "timeline_marker_status_disabled")
    payload = payload.model_copy(deep=True)
    digest = command_hash(payload)
    try:
        ctx = bases_registry.context(db, token, share_id, entry_id)
        receipt = db.get(m.BaseTimelineMarkerReceipt, (ctx.user.id, payload.operation_id))
        result = dict(
            status="unknown", operation_id=str(payload.operation_id), entry_id=str(entry_id)
        )
        if receipt is not None:
            definition = db.get(m.BaseDefinition, receipt.definition_id)
            if definition is None or definition.entry_id != ctx.entry.id:
                error(404, "target_unavailable")
            if (
                receipt.definition_id != payload.definition_id
                or receipt.generation != payload.generation
                or receipt.request_hash != digest
            ):
                error(409, "operation_id_conflict")
            result = dict(status="accepted", result=receipt.result)
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        error(500, "timeline_marker_storage_error")
    except Exception:
        db.rollback()
        raise


def apply_command(db, token, share_id, entry_id, payload, restore_generation=None):
    discussions.require_clean_session(db)
    request_hash = command_hash(payload)
    try:
        ctx, definition = scope(db, token, share_id, entry_id, write=True)
        collaboration_restore.require_generation(ctx.share, restore_generation)
        if payload.definition_id != definition.id or payload.generation != definition.generation:
            error(409, "base_generation_changed")
        receipt = db.get(m.BaseTimelineMarkerReceipt, (ctx.user.id, payload.operation_id))
        if receipt:
            if receipt.definition_id != definition.id or receipt.request_hash != request_hash:
                error(409, "operation_id_conflict")
            if receipt.generation != definition.generation:
                error(409, "base_generation_changed")
            response = receipt.result
            db.commit()
            return response
        if not enabled():
            error(409, "timeline_markers_disabled")
        if definition.content_sha256 != ctx.entry.record.get("hash"):
            error(409, "base_definition_not_synced")
        if payload.registry_revision != definition.revision:
            error(409, "stale_base_revision")
        catalog, catalog_revision = state(db, definition)
        if catalog_revision != payload.expected_catalog_revision:
            error(409, "timeline_marker_catalog_changed")
        command = payload.command
        marker = db.get(m.BaseTimelineMarker, command.marker_id)
        if marker and marker.definition_id != definition.id:
            error(409, "identifier_unavailable")
        if (marker.revision if marker else 0) != command.expected_marker_revision:
            error(409, "timeline_marker_changed")
        if isinstance(command, PutTimelineMarker):
            if marker is None:
                count = db.scalar(
                    select(func.count())
                    .select_from(m.BaseTimelineMarker)
                    .where(m.BaseTimelineMarker.definition_id == definition.id)
                )
                if count >= MAX_MARKERS:
                    error(409, "timeline_marker_limit")
                marker = m.BaseTimelineMarker(
                    id=command.marker_id,
                    definition_id=definition.id,
                    revision=0,
                    active=True,
                    content={},
                )
                db.add(marker)
                db.flush()
            if not base_target_definitions.synchronize(db, definition):
                error(409, "base_definition_targets_limit")
            existing = {item.view_id: item for item in placements(db, marker)}
            if (
                len(set(existing) | {item.view_id for item in command.placements})
                > MAX_RETAINED_PLACEMENTS
            ):
                error(409, "timeline_marker_placement_limit")
            verified = [
                (item, checked_placement(db, definition, item, command.content))
                for item in command.placements
            ]
            for placement in existing.values():
                placement.active = False
            for requested, axis in verified:
                placement = existing.get(requested.view_id)
                if placement is None:
                    placement = m.BaseTimelineMarkerPlacement(
                        marker_id=marker.id, view_id=requested.view_id
                    )
                    db.add(placement)
                placement.active = True
                placement.axis_definition_id = axis.id
                placement.axis_definition_revision = axis.revision
                placement.axis = axis.descriptor["axis"]
            marker.content = command.content.model_dump(mode="json")
            marker.active = True
        else:
            if marker is None:
                error(404, "target_unavailable")
            marker.active = False
            for placement in placements(db, marker):
                placement.active = False
        marker.revision += 1
        if catalog is None:
            catalog = m.BaseTimelineMarkerCatalog(definition_id=definition.id, revision=0)
            db.add(catalog)
        catalog.revision += 1
        db.flush()
        saved = snapshot(db, marker)
        db.add(
            m.BaseTimelineMarkerRevision(
                marker_id=marker.id,
                revision=marker.revision,
                actor_id=ctx.user.id,
                payload=saved,
            )
        )
        response = dict(
            **identity(definition, catalog.revision),
            operation_id=str(payload.operation_id),
            marker=saved,
        )
        db.add(
            m.BaseTimelineMarkerReceipt(
                actor_id=ctx.user.id,
                operation_id=payload.operation_id,
                definition_id=definition.id,
                generation=definition.generation,
                request_hash=request_hash,
                result=response,
            )
        )
        db.commit()
        return response
    except IntegrityError:
        db.rollback()
        error(409, "identifier_unavailable")
    except OperationalError:
        db.rollback()
        error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        error(500, "timeline_marker_storage_error")
    except Exception:
        db.rollback()
        raise


def validate(db, ctx, definition, anchor, *, creation, write, user):
    """All thread reads/replies/receipts/pictures/inbox use this same authority."""
    requested = anchor.target
    marker = db.get(m.BaseTimelineMarker, requested.marker_id)
    revision = db.get(
        m.BaseTimelineMarkerRevision, (requested.marker_id, requested.marker_revision)
    )
    if not marker or marker.definition_id != definition.id or revision is None:
        error(
            409 if creation else 404,
            "timeline_marker_unavailable" if creation else "target_unavailable",
        )
    captured = revision.payload
    placement = next(
        (
            item
            for item in captured["placements"]
            if item["view_id"] == str(anchor.view_id) and item["active"]
        ),
        None,
    )
    if not placement or not captured["active"]:
        error(
            409 if creation else 404,
            "timeline_marker_unavailable" if creation else "target_unavailable",
        )
    if captured["content"] != requested.content.model_dump(mode="json") or any(
        placement[key] != requested.model_dump(mode="json")[key]
        for key in ("axis_definition_id", "axis_definition_revision", "axis")
    ):
        error(422 if creation else 404, "invalid_anchor" if creation else "target_unavailable")
    if creation:
        if not enabled():
            error(409, "timeline_markers_disabled")
        if not marker.active or marker.revision != requested.marker_revision:
            error(409, "timeline_marker_changed")
        # Put/archive advances marker revision atomically with every association.
        # An old, detached placement can remain readable through its snapshot.
        current = db.get(m.BaseTimelineMarkerPlacement, (marker.id, anchor.view_id))
        if not current or not current.active:
            error(409, "timeline_marker_unavailable")
    range_target = ds.BaseTimelineRangeTarget.model_validate(
        dict(
            type="timeline_range",
            axis_definition_id=requested.axis_definition_id,
            axis_definition_revision=requested.axis_definition_revision,
            axis=requested.axis,
            range=requested.content.range,
        )
    )
    base_discussions.validate(
        db,
        ctx,
        anchor.model_copy(update={"target": range_target}),
        creation=creation,
        write=write,
        user=user,
    )

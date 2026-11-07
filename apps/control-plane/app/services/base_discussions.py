"""Saved-view target identity and intersection authorization.

The caller holds the authenticated share lock. This module performs no object
reads, evaluates no formulas/filters and never trusts a client-supplied path or
dependency list. Record dependencies are derived from the typed target.
"""

import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.db import models as m
from app.schemas import discussions as s
from app.services import collaboration_policy, managed_service

# Bases represents files, including attachments. These are row/cell targets, not
# annotation support for PDF pages, Canvas nodes, or other file contents.
RECORD_TYPES = ("markdown", "image", "audio", "video", "pdf", "canvas", "file")


def error(status=404, code="target_unavailable"):
    raise HTTPException(status, {"code": code})


def enabled():
    settings = get_settings()
    return (
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_bases_registry_enabled
        and settings.collaboration_bases_discussions_enabled
    )


def definitions_enabled():
    return enabled() and get_settings().collaboration_bases_definition_targets_enabled


def formula_cells_enabled():
    return definitions_enabled() and get_settings().collaboration_bases_formula_cells_enabled


def timeline_ranges_enabled():
    return enabled() and get_settings().collaboration_bases_timeline_ranges_enabled


def groups_enabled():
    settings = get_settings()
    return bool(
        definitions_enabled()
        and settings.collaboration_bases_groups_enabled
        and settings.collaboration_anchor_relay_token
        and settings.collaboration_anchor_relay_token.get_secret_value()
    )


def definition_for(db, ctx):
    definition = db.scalar(
        select(m.BaseDefinition).where(m.BaseDefinition.entry_id == ctx.entry.id)
    )
    if not definition or definition.generation != ctx.entry.collaboration_generation:
        error()
    return definition


def view_for(db, ctx, view_id, *, active=False):
    definition = definition_for(db, ctx)
    view = db.get(m.BaseView, view_id)
    if not view or view.definition_id != definition.id:
        error()
    if active and view.state != "active":
        error(409, "base_view_unavailable")
    return definition, view


def record_for(db, ctx, record_id, *, user=None, write=False):
    user = user or ctx.user
    entry = db.scalar(
        select(m.ManagedEntry).where(
            m.ManagedEntry.id == str(record_id),
            m.ManagedEntry.share_id == ctx.share.id,
            m.ManagedEntry.deleted.is_(False),
        )
    )
    if not entry or entry.record.get("type") not in RECORD_TYPES:
        error()
    permission = managed_service.permission(
        ctx.share, user, entry.path, managed_service.rules_for(db, ctx.share, user)
    )
    if permission not in {"read", "write"}:
        error()
    if write and not collaboration_policy.can_comment(
        ctx.share, user, entry.path, permission, collaboration_policy.rules_for(db, ctx.share, user)
    ):
        error(403, "comment_forbidden")
    return entry


def validate(db, ctx, anchor, *, creation=False, write=False, user=None):
    if not isinstance(anchor, s.BaseAnchor) or ctx.entry.record.get("type") != "file":
        error(422, "invalid_anchor_scope")
    definition, _view = view_for(db, ctx, anchor.view_id, active=creation)
    if anchor.definition_id != definition.id or anchor.generation != definition.generation:
        error(
            409 if creation else 404,
            "anchor_generation_mismatch" if creation else "target_unavailable",
        )
    if creation:
        if not enabled():
            error(409, "base_discussions_disabled")
        if not ctx.entry.path.lower().endswith(".base"):
            error()
        if definition.content_sha256 != ctx.entry.record.get("hash"):
            error(409, "base_definition_not_synced")
        revision = db.get(m.BaseRegistryRevision, (definition.id, anchor.registry_revision))
        if not revision or not any(
            view.get("id") == str(anchor.view_id) for view in revision.result.get("views", [])
        ):
            error(409, "base_view_unavailable")
    if isinstance(anchor.target, s.BaseItemTarget):
        entry = record_for(db, ctx, anchor.target.record_id, user=user, write=write)
        if entry.collaboration_generation != anchor.target.record_generation:
            error(
                409 if creation else 404,
                "anchor_generation_mismatch" if creation else "target_unavailable",
            )
        if (
            isinstance(anchor.target, s.BaseCellTarget)
            and anchor.target.property.source == "note"
            and entry.record.get("type") != "markdown"
        ):
            error(422 if creation else 404, "invalid_anchor" if creation else "target_unavailable")
    if isinstance(anchor.target, s.BaseDefinedTarget):
        if creation:
            from app.services import base_target_definitions

            if not definitions_enabled():
                error(409, "base_definition_targets_disabled")
            if isinstance(anchor.target, s.BaseFormulaCellTarget) and not formula_cells_enabled():
                error(409, "base_formula_cells_disabled")
            if isinstance(anchor.target, s.BaseGroupTarget) and not groups_enabled():
                error(409, "base_groups_disabled")
            if not base_target_definitions.synchronize(db, definition):
                error(409, "base_definition_targets_limit")
        target = db.get(m.BaseTargetDefinition, anchor.target.definition_id)
        kind = (
            "formula" if isinstance(anchor.target, s.BaseFormulaCellTarget) else anchor.target.type
        )
        if not target or target.view_id != anchor.view_id or target.kind != kind:
            error(
                409 if creation else 404,
                "base_target_definition_unavailable" if creation else "target_unavailable",
            )
        if creation:
            if (
                not target.active
                or target.revision != anchor.target.definition_revision
                or target.revision > anchor.registry_revision
            ):
                error(409, "base_target_definition_changed")
            if (
                target.descriptor.get("property") != anchor.target.property.model_dump(mode="json")
                or isinstance(anchor.target, s.BaseSummaryTarget)
                and target.descriptor.get("summary") != anchor.target.summary
            ):
                error(422, "invalid_anchor")
        # Existing discussions retain their captured definition after edits,
        # removal or disablement. Definition-only authority is the view;
        # formula cells also require record authority and lineage above.
        # Group values additionally require their exact source witness below.
        # None of these targets grants access to a formula's evaluated output.
    if isinstance(anchor.target, s.BaseGroupTarget):
        from app.services import base_groups

        base_groups.authorize(db, ctx, anchor, user=user)
    if isinstance(anchor.target, s.BaseTimelineRangeTarget):
        from app.services import timeline_axes

        timeline_axes.validate(
            db, ctx, definition, anchor, creation=creation, write=write, user=user
        )
    if isinstance(anchor.target, s.BaseTimelineMarkerTarget):
        from app.services import timeline_markers

        timeline_markers.validate(
            db, ctx, definition, anchor, creation=creation, write=write, user=user
        )


def stored_anchor(payload):
    try:
        return s.BaseAnchor.model_validate({k: v for k, v in payload.items() if k != "entry_id"})
    except (ValidationError, AttributeError, TypeError):
        error()


def authorize_thread(db, ctx, thread, *, write=False, user=None):
    """All accesses to a Bases thread, including receipts and pictures, use this."""
    if ctx.entry.record.get("type") != "file":
        return None
    revision = db.get(m.DiscussionAnchorRevision, (thread.id, thread.anchor_revision))
    if not revision:
        error()
    anchor = stored_anchor(revision.payload)
    validate(db, ctx, anchor, write=write, user=user)
    return anchor


def can_read_thread(db, ctx, thread, *, user=None):
    try:
        authorize_thread(db, ctx, thread, user=user)
        return True
    except HTTPException as exc:
        if exc.status_code not in {403, 404}:
            raise
        return False


def target_context(db, token, share_id, entry_id, payload):
    """A reader can capture authoritative lineage without editing vault files."""
    from app.services import bases_registry, discussion_service

    discussion_service.require_clean_session(db)
    try:
        if not enabled():
            error(409, "base_discussions_disabled")
        ctx = bases_registry.context(db, token, share_id, entry_id, write=True)
        definition, _view = view_for(db, ctx, payload.view_id, active=True)
        if definition.content_sha256 != ctx.entry.record.get("hash"):
            error(409, "base_definition_not_synced")
        result = dict(
            protocol_version=1,
            entry_id=ctx.entry.id,
            definition_id=str(definition.id),
            view_id=str(payload.view_id),
            generation=str(definition.generation),
            registry_revision=definition.revision,
        )
        if payload.record_id is not None:
            record = record_for(db, ctx, payload.record_id)
            if record.collaboration_generation is None:
                record.collaboration_generation = uuid.uuid4()
            result.update(
                record_id=record.id, record_generation=str(record.collaboration_generation)
            )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise

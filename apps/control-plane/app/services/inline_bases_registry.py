"""Persist verified inline definitions without modifying their Markdown source."""

import uuid

from sqlalchemy import select

from app.db import models as m
from app.services import base_target_definitions, bases_definition, bases_registry


def synchronize_verified(db, ctx, proof, source_request, expected_revision):
    """Caller holds the share write lock and has rechecked the private proof."""
    definition = db.scalar(
        select(m.BaseDefinition).where(
            m.BaseDefinition.entry_id == ctx.entry.id,
            m.BaseDefinition.source_key == proof.identity_sha256,
        )
    )
    if definition and definition.generation != ctx.entry.collaboration_generation:
        bases_registry.error(409, "base_generation_changed")
    if definition and definition.content_sha256 == proof.definition.sha256:
        return bases_registry.snapshot(db, definition, ctx.share)
    if (definition.revision if definition else 0) != expected_revision:
        bases_registry.error(409, "stale_base_revision")
    if not definition:
        definition = m.BaseDefinition(
            entry_id=ctx.entry.id,
            source_key=proof.identity_sha256,
            source_anchor={"start": source_request.start, "end": source_request.end},
            generation=ctx.entry.collaboration_generation,
            revision=0,
            content_sha256=proof.definition.sha256,
            configuration=proof.definition.value,
        )
        db.add(definition)
        db.flush()
    active = list(
        db.scalars(
            select(m.BaseView).where(
                m.BaseView.definition_id == definition.id,
                m.BaseView.state == "active",
            )
        )
    )
    previous = [
        {"id": str(view.id), "state": view.state, "descriptor": view.descriptor} for view in active
    ]
    unmatched = (
        db.scalar(
            select(m.BaseView.id)
            .where(
                m.BaseView.definition_id == definition.id,
                m.BaseView.state == "unmatched",
            )
            .limit(1)
        )
        is not None
    )
    if unmatched:
        previous.append({"state": "unmatched"})
    bindings, ambiguous, deleted = bases_definition.reconcile(previous, proof.definition.views)
    known = {view.id: view for view in active}
    for view in active:
        view.slot = None
        view.state = "archived" if str(view.id) in deleted else "unmatched"
    db.flush()
    for slot, value in enumerate(proof.definition.views):
        view_id = bindings.get(slot)
        if view_id:
            view = known[uuid.UUID(view_id)]
        elif not ambiguous and not unmatched:
            view = m.BaseView(definition_id=definition.id)
            db.add(view)
        else:
            continue
        view.descriptor, view.slot, view.state = value, slot, "active"
    definition.revision += 1
    definition.content_sha256 = proof.definition.sha256
    definition.configuration = proof.definition.value
    db.flush()
    base_target_definitions.synchronize(db, definition)
    result = bases_registry.snapshot(db, definition, ctx.share)
    db.add(
        m.BaseRegistryRevision(
            definition_id=definition.id,
            revision=definition.revision,
            actor_id=ctx.user.id,
            result=result,
        )
    )
    return result


def revision_for(db, entry_id, source_key):
    return (
        db.scalar(
            select(m.BaseDefinition.revision).where(
                m.BaseDefinition.entry_id == entry_id,
                m.BaseDefinition.source_key == source_key,
            )
        )
        or 0
    )

"""Durable group targets with immutable, thread-selected source evidence.

A matching value from another record may share group identity, but must never
grant access to a thread's different witness. All RPC happens outside SQL locks.
"""

import hashlib
import posixpath
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.db import models as m
from app.schemas import discussions as s
from app.services import (
    base_discussions,
    base_group_values,
    base_source_service,
    base_target_definitions,
    bases_definition,
    discussion_service,
    managed_service,
)

error = discussion_service.error
MAX_GROUPS = 10000
MAX_WITNESSES = 4096
MAX_LINK_INVENTORY = 4096


def _references(db, ctx, evidence, *, user=None):
    """Read every input in this exact witness, never alternative group witnesses."""
    raw = evidence.get("references")
    if (
        type(evidence.get("version")) is not int
        or evidence["version"] != 1
        or evidence.get("adapter") != "note_property_v1"
        or not isinstance(raw, list)
        or not 1 <= len(raw) <= 256
    ):
        error(404, "target_unavailable")
    try:
        refs = {
            str(uuid.UUID(item["record_id"])): uuid.UUID(item["record_generation"]) for item in raw
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        error(404, "target_unavailable")
    if len(refs) != len(raw):
        error(404, "target_unavailable")
    origin = evidence.get("source")
    if (
        not isinstance(origin, dict)
        or origin.get("record_id") not in refs
        or str(refs[origin["record_id"]]) != origin.get("record_generation")
    ):
        error(404, "target_unavailable")
    user = user or ctx.user
    rows = list(
        db.scalars(
            select(m.ManagedEntry).where(
                m.ManagedEntry.id.in_(refs),
                m.ManagedEntry.share_id == ctx.share.id,
                m.ManagedEntry.deleted.is_(False),
            )
        )
    )
    if len(rows) != len(refs):
        error(404, "target_unavailable")
    rules = managed_service.rules_for(db, ctx.share, user)
    for row in rows:
        if (
            row.record.get("type") not in base_discussions.RECORD_TYPES
            or row.id == origin["record_id"]
            and row.record.get("type") != "markdown"
            or row.collaboration_generation != refs[row.id]
            or managed_service.permission(ctx.share, user, row.path, rules) not in {"read", "write"}
        ):
            error(404, "target_unavailable")


def authorize(db, ctx, anchor, *, user=None):
    target = anchor.target
    group = db.get(m.BaseGroupValue, target.group_id)
    witness = db.get(m.BaseGroupWitness, target.witness_id)
    if (
        not group
        or not witness
        or witness.group_id != group.id
        or group.definition_id != target.definition_id
        or group.definition_revision != target.definition_revision
        or group.value != target.value
        or witness.evidence.get("property") != target.property.model_dump(mode="json")
    ):
        error(404, "target_unavailable")
    _references(db, ctx, witness.evidence, user=user)


def _request_hash(share_id, entry_id, payload):
    return bases_definition.fingerprint(
        {
            "share_id": str(share_id),
            "entry_id": str(entry_id),
            "request": payload.model_dump(mode="json"),
        }
    )


def _prior(db, ctx, payload, digest):
    receipt = db.get(m.BaseGroupCaptureReceipt, (ctx.user.id, payload.operation_id))
    if receipt is None:
        return None
    if receipt.request_hash != digest:
        error(409, "operation_conflict")
    anchor = base_discussions.stored_anchor(receipt.result["anchor"])
    if anchor.definition_id != base_discussions.definition_for(db, ctx).id:
        error(404, "target_unavailable")
    base_discussions.validate(db, ctx, anchor)
    return receipt.result


def _capture_context(db, ctx, payload):
    if not base_discussions.groups_enabled():
        error(409, "base_groups_disabled")
    definition, view = base_discussions.view_for(db, ctx, payload.view_id, active=True)
    if definition.id != payload.definition_id or definition.generation != payload.generation:
        error(409, "anchor_generation_mismatch")
    if not ctx.entry.path.lower().endswith(
        ".base"
    ) or definition.content_sha256 != ctx.entry.record.get("hash"):
        error(409, "base_definition_not_synced")
    revision = db.get(m.BaseRegistryRevision, (definition.id, payload.registry_revision))
    if not revision or not any(
        v.get("id") == str(view.id) for v in revision.result.get("views", [])
    ):
        error(409, "base_view_unavailable")
    if not base_target_definitions.synchronize(db, definition):
        error(409, "base_definition_targets_limit")
    grouping = db.get(m.BaseTargetDefinition, payload.group_definition_id)
    if not grouping or grouping.kind != "group" or grouping.view_id != view.id:
        error(409, "base_target_definition_unavailable")
    if (
        not grouping.active
        or grouping.revision != payload.group_definition_revision
        or grouping.revision > payload.registry_revision
    ):
        error(409, "base_target_definition_changed")
    prop = grouping.descriptor.get("property", {})
    if prop.get("source") != "note" or set(view.descriptor.get("groupBy", {})) - {
        "property",
        "direction",
    }:
        error(409, "base_group_adapter_unavailable")
    source = base_discussions.record_for(db, ctx, payload.record_id)
    if source.collaboration_generation != payload.record_generation:
        error(409, "anchor_generation_mismatch")
    if source.record.get("type") != "markdown":
        error(409, "base_group_adapter_unavailable")
    return definition, grouping, source


def _link_resolver(db, ctx):
    inventory = None

    def resolve(link, source_path):
        nonlocal inventory
        if inventory is None:
            inventory = list(
                db.scalars(
                    select(m.ManagedEntry)
                    .where(
                        m.ManagedEntry.share_id == ctx.share.id, m.ManagedEntry.deleted.is_(False)
                    )
                    .order_by(m.ManagedEntry.id)
                    .limit(MAX_LINK_INVENTORY + 1)
                )
            )
            if len(inventory) > MAX_LINK_INVENTORY:
                raise base_group_values.UnavailableValue("Link inventory exceeds the adapter limit")
        if "\\" in link or "\x00" in link:
            raise base_group_values.UnavailableValue("Unsupported link path")
        if not link:
            paths = {source_path}
        elif link.startswith(("./", "../")):
            path = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), link))
            if path == ".." or path.startswith("../"):
                raise base_group_values.UnavailableValue("Link escapes the managed share")
            paths = {path, path + ".md"}
        else:
            path = posixpath.normpath(link.lstrip("/"))
            if path == ".." or path.startswith("../"):
                raise base_group_values.UnavailableValue("Link escapes the managed share")
            paths = {path, path + ".md"}
            prefix = ctx.share.path.strip("/") + "/"
            if path.startswith(prefix):
                paths |= {path[len(prefix) :], path[len(prefix) :] + ".md"}
        matches = [
            row
            for row in inventory
            if row.path in paths
            or (
                not link.startswith(("/", "./", "../"))
                and any(row.path.endswith("/" + p) for p in paths)
            )
        ]
        if len(matches) > 1:
            raise base_group_values.UnavailableValue("Ambiguous managed link")
        if not matches:
            return None
        row = matches[0]
        base_discussions.record_for(db, ctx, uuid.UUID(row.id))
        if row.collaboration_generation is None:
            # Lineage is normally initialized through target-context. Never
            # accept a client generation or mutate a referenced record here.
            raise base_group_values.UnavailableValue("Link lineage is unavailable")
        return base_group_values.RecordIdentity(uuid.UUID(row.id), row.collaboration_generation)

    return resolve


def capture(db, token, share_id, entry_id, payload):
    discussion_service.require_clean_session(db)
    payload = payload.model_copy(deep=True)
    digest = _request_hash(share_id, entry_id, payload)
    try:
        ctx = discussion_service.context(
            db, token, share_id, entry_id, write=True, entry_types={"file"}
        )
        prior = _prior(db, ctx, payload, digest)
        if prior is not None:
            db.commit()
            return prior
        _, _, source = _capture_context(db, ctx, payload)
        # Copy primitives before commit; no ORM lazy load may reopen SQL while
        # the private network request is in progress.
        source_path = source.path
        db.commit()
        observed = base_source_service.observe(
            share_id, payload.record_id, payload.record_generation, payload.dependency
        )
        ctx = discussion_service.context(
            db, token, share_id, entry_id, write=True, entry_types={"file"}
        )
        prior = _prior(db, ctx, payload, digest)
        if prior is not None:
            db.commit()
            return prior
        definition, grouping, source = _capture_context(db, ctx, payload)
        if source.path != source_path:
            error(409, "base_group_source_changed")
        try:
            derived = base_group_values.derive_note_value(
                observed,
                grouping.descriptor["property"]["name"],
                base_group_values.RecordIdentity(payload.record_id, payload.record_generation),
                share_id=share_id,
                dependency=payload.dependency,
                source_path=source_path,
                time_zone=payload.time_zone,
                resolve_link=_link_resolver(db, ctx),
            )
        except base_group_values.UnavailableValue:
            error(409, "base_group_value_unavailable")
        if derived.value != payload.value:
            error(409, "base_group_value_changed")
        references = sorted(
            (ref.wire() for ref in derived.references), key=lambda r: r["record_id"]
        )
        evidence = {
            "version": 1,
            "adapter": "note_property_v1",
            "property": grouping.descriptor["property"],
            "source": {
                "record_id": str(payload.record_id),
                "record_generation": str(payload.record_generation),
            },
            "source_path_sha256": hashlib.sha256(source_path.encode()).hexdigest(),
            "time_zone": payload.time_zone,
            "references": references,
        }
        _references(db, ctx, evidence)
        group = db.scalar(
            select(m.BaseGroupValue).where(
                m.BaseGroupValue.definition_id == grouping.id,
                m.BaseGroupValue.definition_revision == grouping.revision,
                m.BaseGroupValue.fingerprint == derived.fingerprint,
            )
        )
        if group is None:
            count = db.scalar(
                select(func.count())
                .select_from(m.BaseGroupValue)
                .where(m.BaseGroupValue.definition_id == grouping.id)
            )
            if count >= MAX_GROUPS:
                error(409, "base_group_limit")
            group = m.BaseGroupValue(
                definition_id=grouping.id,
                definition_revision=grouping.revision,
                fingerprint=derived.fingerprint,
                value=derived.value,
            )
            db.add(group)
            db.flush()
        elif group.value != derived.value:
            error(409, "base_group_identity_conflict")
        witness_hash = bases_definition.fingerprint(evidence)
        witness = db.scalar(
            select(m.BaseGroupWitness).where(
                m.BaseGroupWitness.group_id == group.id,
                m.BaseGroupWitness.fingerprint == witness_hash,
            )
        )
        if witness is None:
            count = db.scalar(
                select(func.count())
                .select_from(m.BaseGroupWitness)
                .where(m.BaseGroupWitness.group_id == group.id)
            )
            if count >= MAX_WITNESSES:
                error(409, "base_group_witness_limit")
            witness = m.BaseGroupWitness(
                group_id=group.id,
                fingerprint=witness_hash,
                evidence={
                    **evidence,
                    "source_identity_sha256": observed.identity_sha256,
                    "source_snapshot_sha256": observed.observed_snapshot_sha256,
                    "source_prefix_sha256": observed.source_prefix_sha256,
                },
            )
            db.add(witness)
            db.flush()
        target = s.BaseGroupTarget(
            type="group",
            definition_id=grouping.id,
            definition_revision=grouping.revision,
            property=grouping.descriptor["property"],
            group_id=group.id,
            witness_id=witness.id,
            value=group.value,
        )
        anchor = s.BaseAnchor(
            version=1,
            type="base",
            definition_id=definition.id,
            view_id=payload.view_id,
            generation=definition.generation,
            registry_revision=definition.revision,
            target=target,
        )
        result = {
            "protocol_version": 1,
            "entry_id": str(entry_id),
            "anchor": anchor.model_dump(mode="json"),
        }
        db.add(
            m.BaseGroupCaptureReceipt(
                actor_id=ctx.user.id,
                operation_id=payload.operation_id,
                witness_id=witness.id,
                request_hash=digest,
                result=result,
            )
        )
        # No expired evidence can be turned into a durable capture receipt.
        observed.check(share_id, payload.record_id, payload.record_generation, payload.dependency)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        error(409, "identifier_unavailable")
    except OperationalError:
        db.rollback()
        error(503, "retryable_transaction")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_group_storage_error")
    except Exception:
        db.rollback()
        raise

"""Server-owned Bases/view identities over verified bytes, outside vault files.

All mutations serialize on the share, like the managed inventory. Object I/O
and YAML parsing happen after releasing SQL, then identity/hash/permission are
checked again. Read-only members can inspect; only base editors can repair.
"""

import hashlib
import re
import threading
import uuid

from minio.error import S3Error
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.clients import storage_client
from app.core.config import get_settings
from app.db import models as m
from app.schemas.bases import ReconcileBase
from app.services import (
    base_content,
    base_target_definitions,
    collaboration_restore,
    discussion_service,
    managed_service,
)
from app.services import bases_definition as codec

error = discussion_service.error
_read_slots = threading.BoundedSemaphore(2)


def enabled():
    settings = get_settings()
    return settings.collaboration_policy_enabled and settings.collaboration_bases_registry_enabled


def context(db, token, share_id, entry_id, *, write=False, edit=False):
    ctx = discussion_service.context(
        db, token, share_id, entry_id, write=write, entry_types={"file"}, require_comment=False
    )
    if not ctx.entry.path.lower().endswith(".base"):
        error(404, "target_unavailable")
    if (
        edit
        and managed_service.permission(
            ctx.share, ctx.user, ctx.entry.path, managed_service.rules_for(db, ctx.share, ctx.user)
        )
        != "write"
    ):
        error(403, "base_editor_required")
    return ctx


def definition_for(db, ctx):
    definition = db.scalar(
        select(m.BaseDefinition).where(m.BaseDefinition.entry_id == ctx.entry.id)
    )
    if definition and definition.generation != ctx.entry.collaboration_generation:
        error(409, "base_generation_changed")
    return definition


def descriptor(view):
    return {
        "name": view["name"],
        "type": view["type"],
        "descriptor_sha256": codec.fingerprint(view),
    }


def view_page(db, definition, state, after=None, limit=100):
    query = select(m.BaseView).where(
        m.BaseView.definition_id == definition.id, m.BaseView.state == state
    )
    if after:
        query = query.where(m.BaseView.id > after)
    rows = list(db.scalars(query.order_by(m.BaseView.id).limit(limit + 1)))
    page = rows[:limit]
    return {
        "views": [
            {"id": str(view.id), "state": view.state, **descriptor(view.descriptor)}
            for view in page
        ],
        "has_more": len(rows) > limit,
        "next_after": str(page[-1].id) if page else (str(after) if after else None),
    }


def snapshot(db, definition, share):
    active = {
        view.slot: view
        for view in db.scalars(
            select(m.BaseView).where(
                m.BaseView.definition_id == definition.id, m.BaseView.state == "active"
            )
        )
    }
    return {
        "protocol_version": 1,
        "restore_guard": True,
        "restore_generation": (
            str(share.collaboration_restore_generation)
            if share.collaboration_restore_generation
            else None
        ),
        "definition_id": str(definition.id),
        "entry_id": definition.entry_id,
        "generation": str(definition.generation),
        "revision": definition.revision,
        "content_sha256": definition.content_sha256,
        "views": [
            {
                "slot": slot,
                "id": str(active[slot].id) if slot in active else None,
                **descriptor(view),
            }
            for slot, view in enumerate(definition.configuration["views"])
        ],
        "unmatched": view_page(db, definition, "unmatched"),
    }


def verified_definition(share_id, entry_id, sha256, stored=None):
    if not _read_slots.acquire(blocking=False):
        error(429, "base_inspection_busy")
    try:
        settings = get_settings()
        try:
            with storage_client.storage_errors("bases_inspect"):
                if stored is not None:
                    raw = stored
                else:
                    response = storage_client.get_minio_client().get_object(
                        settings.minio_bucket, f"web-assets/{share_id}/{entry_id}"
                    )
                    try:
                        raw = response.read(codec.MAX_BYTES + 1)
                    finally:
                        response.close()
                        response.release_conn()
        except S3Error as exc:
            error(
                409 if exc.code in {"NoSuchKey", "NoSuchObject"} else 503,
                "base_definition_unavailable",
            )
        if len(raw) > codec.MAX_BYTES:
            error(413, "base_definition_too_large")
        if hashlib.sha256(raw).hexdigest() != sha256:
            error(409, "base_definition_not_synced")
        try:
            return codec.parse(raw)
        except codec.InvalidBase:
            error(422, "invalid_base_definition")
    finally:
        _read_slots.release()


def existing_receipt(db, ctx, definition, payload, request_hash):
    if not isinstance(payload, ReconcileBase):
        return None
    receipt = db.scalar(
        select(m.BaseRegistryRevision).where(
            m.BaseRegistryRevision.actor_id == ctx.user.id,
            m.BaseRegistryRevision.operation_id == payload.operation_id,
        )
    )
    if receipt is None:
        return None
    if (
        not definition
        or receipt.definition_id != definition.id
        or receipt.request_hash != request_hash
    ):
        error(409, "operation_id_conflict")
    result = dict(receipt.result)
    db.commit()
    return result


def synchronize(db, token, share_id, entry_id, payload, restore_generation=None):
    discussion_service.require_clean_session(db)
    payload = payload.model_copy(deep=True)
    manual = isinstance(payload, ReconcileBase)
    request_hash = codec.fingerprint(
        {"share_id": str(share_id), "entry_id": str(entry_id), **payload.model_dump(mode="json")}
    )
    try:
        ctx = context(db, token, share_id, entry_id, edit=manual)
        if manual:
            collaboration_restore.require_generation(ctx.share, restore_generation)
        observed_restore = ctx.share.collaboration_restore_generation
        definition = definition_for(db, ctx)
        prior = existing_receipt(db, ctx, definition, payload, request_hash)
        if prior is not None:
            return prior
        expected_hash = ctx.entry.record.get("hash")
        if (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or expected_hash != payload.content_sha256
        ):
            error(409, "base_definition_not_synced")
        if not manual and definition and definition.content_sha256 == expected_hash:
            result = snapshot(db, definition, ctx.share)
            db.commit()
            return result
        generation = ctx.entry.collaboration_generation
        content = base_content.stored_content(db, ctx.entry)
        stored = bytes(content.data) if content else None
        db.commit()  # No SQL connection/row lock during storage or YAML parsing.
        verified = verified_definition(share_id, entry_id, expected_hash, stored)
        ctx = context(db, token, share_id, entry_id, write=True, edit=manual)
        # Inspection derives state from current bytes, but its unlocked object
        # read must not span a restore. Manual repairs retain their own binding.
        collaboration_restore.require_generation(ctx.share, observed_restore)
        if (
            ctx.entry.record.get("hash") != expected_hash
            or ctx.entry.collaboration_generation != generation
        ):
            error(409, "base_definition_changed")
        definition = definition_for(db, ctx)
        prior = existing_receipt(db, ctx, definition, payload, request_hash)
        if prior is not None:
            return prior
        if not manual and definition and definition.content_sha256 == expected_hash:
            result = snapshot(db, definition, ctx.share)
            db.commit()
            return result
        if manual and (definition.revision if definition else 0) != payload.expected_revision:
            error(409, "stale_base_revision")
        if not definition:
            if generation is None:
                generation = ctx.entry.collaboration_generation = uuid.uuid4()
            definition = m.BaseDefinition(
                entry_id=ctx.entry.id,
                generation=generation,
                revision=0,
                content_sha256=expected_hash,
                configuration=verified.value,
            )
            db.add(definition)
            db.flush()
        active = list(
            db.scalars(
                select(m.BaseView).where(
                    m.BaseView.definition_id == definition.id, m.BaseView.state == "active"
                )
            )
        )
        unmatched = (
            db.scalar(
                select(m.BaseView.id)
                .where(m.BaseView.definition_id == definition.id, m.BaseView.state == "unmatched")
                .limit(1)
            )
            is not None
        )
        if manual:
            if sorted(binding.slot for binding in payload.bindings) != list(
                range(len(verified.views))
            ):
                error(422, "incomplete_base_bindings")
            bindings = {
                binding.slot: str(binding.view_id) if binding.view_id else None
                for binding in payload.bindings
            }
            requested = {binding.view_id for binding in payload.bindings if binding.view_id} | set(
                payload.archive
            )
            known = (
                {
                    view.id: view
                    for view in db.scalars(
                        select(m.BaseView).where(
                            m.BaseView.definition_id == definition.id, m.BaseView.id.in_(requested)
                        )
                    )
                }
                if requested
                else {}
            )
            if set(known) != requested:
                error(404, "base_view_unavailable")
            archived = {str(view_id) for view_id in payload.archive}
            ambiguous = set()
            create_new = True
        else:
            previous = [
                {"id": str(view.id), "state": view.state, "descriptor": view.descriptor}
                for view in active
            ]
            if unmatched:
                previous.append({"state": "unmatched"})
            bindings, ambiguous, deleted = codec.reconcile(previous, verified.views)
            archived = set(deleted)
            create_new = not ambiguous and not unmatched
            known = {view.id: view for view in active}
        # Free every old slot before assigning reordered slots; SQL uniqueness
        # must not depend on ORM update order. All rows remain in this transaction.
        for view in active:
            view.slot = None
            view.state = "archived" if str(view.id) in archived else "unmatched"
        for view in known.values():
            if str(view.id) in archived:
                view.slot = None
                view.state = "archived"
        db.flush()
        for slot, value in enumerate(verified.views):
            view_id = bindings.get(slot)
            if view_id:
                view = known[uuid.UUID(view_id)]
            elif create_new:
                view = m.BaseView(definition_id=definition.id)
                db.add(view)
            else:
                continue
            view.descriptor, view.slot, view.state = value, slot, "active"
        definition.revision += 1
        definition.content_sha256, definition.configuration = expected_hash, verified.value
        db.flush()
        base_target_definitions.synchronize(db, definition)
        result = snapshot(db, definition, ctx.share)
        db.add(
            m.BaseRegistryRevision(
                definition_id=definition.id,
                revision=definition.revision,
                actor_id=ctx.user.id,
                operation_id=payload.operation_id if manual else None,
                request_hash=request_hash if manual else None,
                result=result,
            )
        )
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        error(409, "base_registry_conflict")
    except OperationalError:
        db.rollback()
        error(503, "base_registry_unavailable")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_registry_unavailable")
    except Exception:
        db.rollback()
        raise


def repair_status(db, token, share_id, entry_id, payload):
    """Read an actor-owned exact receipt without replaying the repair.

    Current read access is required. A historical receipt does not depend on
    current view/source identity or edit access and never initializes a registry.
    """
    request_hash = codec.fingerprint(
        {"share_id": str(share_id), "entry_id": str(entry_id), **payload.model_dump(mode="json")}
    )
    try:
        ctx = discussion_service.context(
            db, token, share_id, entry_id, entry_types={"file"}, require_comment=False
        )
        if (
            not ctx.entry.path.lower().endswith(".base")
            and db.scalar(
                select(m.BaseDefinition.id).where(m.BaseDefinition.entry_id == ctx.entry.id)
            )
            is None
        ):
            error(404, "target_unavailable")
        receipt = db.scalar(
            select(m.BaseRegistryRevision).where(
                m.BaseRegistryRevision.actor_id == ctx.user.id,
                m.BaseRegistryRevision.operation_id == payload.operation_id,
            )
        )
        if receipt:
            definition = db.get(m.BaseDefinition, receipt.definition_id)
            if not definition or definition.entry_id != ctx.entry.id:
                error(404, "target_unavailable")
            if receipt.request_hash != request_hash:
                error(409, "operation_id_conflict")
            result = {"status": "accepted", "result": dict(receipt.result)}
        else:
            result = {
                "status": "unknown",
                "operation_id": str(payload.operation_id),
                "entry_id": ctx.entry.id,
            }
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        error(503, "base_registry_unavailable")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_registry_unavailable")
    except Exception:
        db.rollback()
        raise


def list_views(db, token, share_id, entry_id, state, after=None):
    try:
        ctx = context(db, token, share_id, entry_id)
        definition = definition_for(db, ctx)
        if not definition:
            error(404, "base_registry_unavailable")
        result = view_page(db, definition, state, after)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise

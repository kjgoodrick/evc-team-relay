"""Small configuration bytes and their publication pointer commit together.

Legacy object reads never hold SQL locks. The guarded path stores at most
256 KiB per file in PostgreSQL, avoiding a distributed object/index commit.
Receipts retain intent/result metadata, not another copy of the file bytes.
"""

import base64
import binascii
import hashlib
import threading
import uuid

from minio.error import S3Error
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.clients import storage_client
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.base_content import MAX_BYTES
from app.services import (
    bases_definition,
    collaboration_restore,
    discussion_service,
    managed_assets,
    managed_service,
)

error = discussion_service.error
_legacy_reads = threading.BoundedSemaphore(2)


def enabled():
    return get_settings().managed_bases_sync_enabled


def is_base(path):
    return path.lower().endswith(".base")


def stored_content(db, entry):
    content = db.get(m.ManagedBaseContent, entry.id)
    if content and (
        content.generation != entry.collaboration_generation
        or content.sha256 != entry.record.get("hash")
        or entry.record.get("type") != "file"
    ):
        error(409, "base_content_lineage_changed")
    return content


def context(db, token, share_id, entry_id, *, write=False):
    ctx = discussion_service.context(
        db, token, share_id, entry_id, write=write, entry_types={"file"}, require_comment=False
    )
    content = stored_content(db, ctx.entry)
    if not is_base(ctx.entry.path) and content is None:
        error(404, "target_unavailable")
    if (
        write
        and managed_service.permission(
            ctx.share, ctx.user, ctx.entry.path, managed_service.rules_for(db, ctx.share, ctx.user)
        )
        != "write"
    ):
        error(403, "base_editor_required")
    return ctx, content


def state(entry, content):
    return {
        "path": entry.path,
        "record": dict(entry.record),
        "generation": str(entry.collaboration_generation)
        if entry.collaboration_generation
        else None,
        "content_version": str(content.version) if content else None,
    }


def read_legacy(share_id, entry_id, expected_hash):
    if not _legacy_reads.acquire(blocking=False):
        error(429, "base_content_busy")
    try:
        try:
            with storage_client.storage_errors("base_content_read"):
                response, _ = managed_assets.open_object(
                    storage_client.get_minio_client(),
                    get_settings().minio_bucket,
                    share_id,
                    entry_id,
                    expected_hash,
                )
                try:
                    raw = response.read(MAX_BYTES + 1)
                finally:
                    response.close()
                    response.release_conn()
        except S3Error as exc:
            error(
                409 if exc.code in {"NoSuchKey", "NoSuchObject"} else 503,
                "base_content_unavailable",
            )
        if len(raw) > MAX_BYTES:
            error(413, "base_content_too_large")
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            error(409, "base_content_not_synced")
        return raw
    finally:
        _legacy_reads.release()


def read(db, token, share_id, entry_id):
    try:
        ctx, content = context(db, token, share_id, entry_id)
        current = state(ctx.entry, content)
        if content:
            raw = bytes(content.data)
        elif not ctx.entry.record.get("hash"):
            # A claimed, not-yet-published entry is distinct from an empty file.
            raw = None
        else:
            db.commit()
            raw = read_legacy(share_id, entry_id, current["record"]["hash"])
            ctx, content = context(db, token, share_id, entry_id)
            if state(ctx.entry, content) != current:
                error(409, "base_content_changed")
        result = {
            "protocol_version": 1,
            "restore_guard": True,
            "restore_generation": str(ctx.share.collaboration_restore_generation)
            if ctx.share.collaboration_restore_generation is not None
            else None,
            "state": current,
            "content_base64": base64.b64encode(raw).decode() if raw is not None else None,
        }
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def prepare(share_id, entry_id, payload):
    # Decode/hash before acquiring database locks. The HTTP route bounds input.
    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error):
        error(422, "invalid_base_content")
    if len(raw) > MAX_BYTES:
        error(413, "base_content_too_large")
    sha256 = hashlib.sha256(raw).hexdigest()
    expected = payload.expected.model_dump(mode="json")
    expected["record"] = payload.expected.record.wire()
    request_hash = bases_definition.fingerprint(
        {
            "share_id": str(share_id),
            "entry_id": str(entry_id),
            "expected": expected,
            "sha256": sha256,
        }
    )
    return raw, sha256, expected, request_hash


def receipt_for(db, ctx, payload, request_hash):
    receipt = db.get(m.ManagedBaseReceipt, (ctx.user.id, payload.operation_id))
    if receipt:
        if receipt.entry_id != ctx.entry.id or receipt.request_hash != request_hash:
            error(409, "operation_id_conflict")
        if not receipt.result.get("withdrawn") and receipt.result["state"]["generation"] != str(
            ctx.entry.collaboration_generation
        ):
            error(409, "base_content_lineage_changed")
    return receipt


def publish(db, token, share_id, entry_id, payload, restore_generation=None):
    raw, sha256, expected, request_hash = prepare(share_id, entry_id, payload)
    try:
        ctx, content = context(db, token, share_id, entry_id, write=True)
        collaboration_restore.require_generation(ctx.share, restore_generation)
        receipt = receipt_for(db, ctx, payload, request_hash)
        if receipt:
            if receipt.result.get("withdrawn"):
                error(410, "base_content_withdrawn")
            result = dict(receipt.result)
            db.commit()
            return result
        if state(ctx.entry, content) != expected:
            error(409, "base_content_conflict")
        if content is None:
            if ctx.entry.collaboration_generation is None:
                ctx.entry.collaboration_generation = uuid.uuid4()
            content = m.ManagedBaseContent(entry_id=ctx.entry.id)
            db.add(content)
        content.version = uuid.uuid4()
        content.generation = ctx.entry.collaboration_generation
        content.sha256, content.data = sha256, raw
        ctx.entry.record = {
            "id": ctx.entry.id,
            "version": 0,
            "type": "file",
            "mimetype": "application/yaml",
            "hash": sha256,
            "synctime": int(security.utcnow().timestamp() * 1000),
        }
        result = {
            "protocol_version": 1,
            "operation_id": str(payload.operation_id),
            "state": state(ctx.entry, content),
        }
        db.add(
            m.ManagedBaseReceipt(
                actor_id=ctx.user.id,
                operation_id=payload.operation_id,
                entry_id=ctx.entry.id,
                request_hash=request_hash,
                result=result,
            )
        )
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        error(409, "base_content_conflict")
    except OperationalError:
        db.rollback()
        error(503, "base_content_unavailable")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_content_unavailable")
    except Exception:
        db.rollback()
        raise


def reject_legacy_write(db, share, entry_id):
    if not share.managed:
        return
    entry = db.get(m.ManagedEntry, entry_id)
    if entry and ((enabled() and is_base(entry.path)) or db.get(m.ManagedBaseContent, entry_id)):
        error(428, "base_content_protocol_required")


def withdraw(db, token, share_id, entry_id, payload, restore_generation=None):
    _, _, _, request_hash = prepare(share_id, entry_id, payload)
    try:
        ctx, _ = context(db, token, share_id, entry_id, write=True)
        collaboration_restore.require_generation(ctx.share, restore_generation)
        receipt = receipt_for(db, ctx, payload, request_hash)
        if receipt and not receipt.result.get("withdrawn"):
            result = {"status": "accepted", "result": dict(receipt.result)}
        else:
            if receipt is None:
                db.add(
                    m.ManagedBaseReceipt(
                        actor_id=ctx.user.id,
                        operation_id=payload.operation_id,
                        entry_id=ctx.entry.id,
                        request_hash=request_hash,
                        result={"withdrawn": True},
                    )
                )
            result = {
                "status": "withdrawn",
                "operation_id": str(payload.operation_id),
                "entry_id": ctx.entry.id,
            }
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        error(409, "operation_id_conflict")
    except OperationalError:
        db.rollback()
        error(503, "base_content_unavailable")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_content_unavailable")
    except Exception:
        db.rollback()
        raise


def command_status(db, token, share_id, entry_id, payload):
    """Inspect an exact historical receipt without replay or withdrawal.

    Current read access is required; current edit permission, source generation
    and content availability do not determine whether the old request committed.
    Unknown never authorizes replacing or discarding that request.
    """
    _, _, _, request_hash = prepare(share_id, entry_id, payload)
    try:
        ctx = discussion_service.context(
            db, token, share_id, entry_id, entry_types={"file"}, require_comment=False
        )
        if not is_base(ctx.entry.path) and db.get(m.ManagedBaseContent, ctx.entry.id) is None:
            error(404, "target_unavailable")
        receipt = db.get(m.ManagedBaseReceipt, (ctx.user.id, payload.operation_id))
        if receipt and receipt.entry_id != ctx.entry.id:
            error(404, "target_unavailable")
        if receipt and receipt.request_hash != request_hash:
            error(409, "operation_id_conflict")
        if receipt and not receipt.result.get("withdrawn"):
            result = {"status": "accepted", "result": dict(receipt.result)}
        else:
            result = {
                "status": "withdrawn" if receipt else "unknown",
                "operation_id": str(payload.operation_id),
                "entry_id": ctx.entry.id,
            }
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        error(503, "base_content_unavailable")
    except SQLAlchemyError:
        db.rollback()
        error(500, "base_content_unavailable")
    except Exception:
        db.rollback()
        raise


def compatible_bytes(db, share, entry_id, token_payload, *, metadata_only=False):
    """Called only after the legacy endpoint's current read authorization."""
    if not share.managed:
        return None
    entry = db.get(m.ManagedEntry, entry_id)
    content = stored_content(db, entry) if entry else None
    if content is None:
        return None
    if token_payload.get("sha256") != content.sha256:
        error(409, "base_content_changed")
    return b"" if metadata_only else bytes(content.data)


def guard_inventory(db, entries, changes):
    """Structural moves/deletes remain supported; metadata cannot bypass CAS."""
    ids = {str(change.value.id) for change in changes.values() if change.value}
    protected = (
        set(
            db.scalars(
                select(m.ManagedBaseContent.entry_id).where(m.ManagedBaseContent.entry_id.in_(ids))
            )
        )
        if ids
        else set()
    )
    for path, change in changes.items():
        if change.value is None:
            continue
        value = change.value.wire()
        existing = entries.get(value["id"])
        guarded = value["id"] in protected or (
            enabled() and (is_base(path) or (existing and is_base(existing.path)))
        )
        if guarded and (value != existing.record if existing else bool(value.get("hash"))):
            error(409, "base_content_protocol_required")


def manifest_versions(db, share_id, visible, visible_removed=()):
    visible_ids = {entry.id for entry in visible}
    removed_paths = {entry.id: entry.last_path for entry in visible_removed}
    rows = list(
        db.execute(
            select(m.ManagedBaseContent.entry_id, m.ManagedBaseContent.version)
            .join(m.ManagedEntry, m.ManagedEntry.id == m.ManagedBaseContent.entry_id)
            .where(m.ManagedEntry.share_id == share_id)
        )
    )
    return {
        "protocol_version": 1,
        "enabled": enabled(),
        "recovery": True,
        "versions": {
            entry_id: str(version) for entry_id, version in rows if entry_id in visible_ids
        },
        # A renamed-away guarded file remains protected after deletion/restart.
        # This exposes no bytes or history and uses the same current path ACL as
        # the ordinary tombstone. Absence is never evidence of deletion.
        "retired": {
            entry_id: removed_paths[entry_id]
            for entry_id, _version in rows
            if entry_id in removed_paths
        },
    }

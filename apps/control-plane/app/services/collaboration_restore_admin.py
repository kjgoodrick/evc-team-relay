"""Explicit offline generation rotation. Not exposed through the HTTP service.

The maintenance caller must keep the restored stack isolated. Table locks make
the metadata transition atomic; they do not provide a deployment admission gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.db import models as m

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class RestoreError(ValueError):
    pass


class ShareState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: uuid.UUID
    generation: uuid.UUID | None
    epoch: uuid.UUID | None
    sequence: int = Field(ge=0, strict=True)


class RestorePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    generation: uuid.UUID
    snapshot_sha256: Sha256
    target_sha256: Sha256
    operator: str = Field(min_length=1, max_length=200)
    schema_versions: tuple[str, ...] = Field(min_length=1, max_length=16)
    shares: tuple[ShareState, ...] = Field(max_length=10000)

    @field_validator("operator")
    @classmethod
    def valid_operator(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid operator label")
        return value

    @field_validator("generation")
    @classmethod
    def fresh_uuid(cls, value: uuid.UUID) -> uuid.UUID:
        if value.version != 4:
            raise ValueError("Restore generation must be a new version-4 UUID")
        return value

    @field_validator("schema_versions")
    @classmethod
    def valid_versions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            not value or len(value) > 64 or not value.replace("_", "").isalnum() for value in values
        ):
            raise ValueError("Invalid schema version inventory")
        return values

    @field_validator("shares")
    @classmethod
    def ordered_unique_shares(cls, values: tuple[ShareState, ...]) -> tuple[ShareState, ...]:
        ids = [str(value.id) for value in values]
        if ids != sorted(set(ids)):
            raise ValueError("Share inventory must be ordered and unique")
        return values


def checksum(value) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    ).hexdigest()


def inventory(db: Session) -> tuple[ShareState, ...]:
    rows = db.execute(
        select(
            m.Share.id,
            m.Share.collaboration_restore_generation,
            m.Share.collaboration_epoch,
            m.Share.collaboration_sequence,
        )
        .order_by(m.Share.id)
        .limit(10001)
    ).all()
    if len(rows) > 10000:
        raise RestoreError("Restore inventory exceeds the maintenance plan limit")
    return tuple(
        ShareState(id=row[0], generation=row[1], epoch=row[2], sequence=row[3]) for row in rows
    )


def prepare(
    db: Session,
    snapshot_sha256: str,
    target_sha256: str,
    operator: str,
    schema_versions: tuple[str, ...],
) -> RestorePlan:
    return RestorePlan(
        generation=uuid.uuid4(),
        snapshot_sha256=snapshot_sha256,
        target_sha256=target_sha256,
        operator=operator,
        schema_versions=schema_versions,
        shares=inventory(db),
    )


def new_epoch(plan: RestorePlan, share_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(plan.generation, f"discussion-cursor:{share_id}")


def verify_receipt(receipt: m.CollaborationRestore, plan: RestorePlan) -> None:
    if (
        receipt.plan_sha256 != checksum(plan.model_dump(mode="json"))
        or receipt.snapshot_sha256 != plan.snapshot_sha256
        or receipt.target_sha256 != plan.target_sha256
        or receipt.previous_state_sha256
        != checksum([item.model_dump(mode="json") for item in plan.shares])
        or receipt.share_count != len(plan.shares)
        or receipt.schema_versions != list(plan.schema_versions)
        or receipt.operator != plan.operator
    ):
        raise RestoreError("Restore receipt belongs to a different plan or is inconsistent")


def apply(db: Session, plan: RestorePlan, schema_versions: tuple[str, ...]) -> dict:
    """Caller owns commit/rollback; no mutations outside this transaction."""
    if db.new or db.dirty or db.deleted:
        raise RestoreError("Restore rotation requires a session without pending changes")
    if tuple(schema_versions) != plan.schema_versions:
        raise RestoreError("Database schema changed since restore review")
    # Whole-table locks also serialize zero-share restores and prevent new-share
    # phantoms. Row locks alone cannot make a complete service inventory atomic.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        db.execute(text("LOCK TABLE shares, collaboration_restores IN ACCESS EXCLUSIVE MODE"))
    elif db.bind.dialect.name != "sqlite":
        raise RestoreError("Unsupported restore database")
    fingerprint = checksum(plan.model_dump(mode="json"))
    receipt = db.get(m.CollaborationRestore, plan.generation, populate_existing=True)
    current = inventory(db)
    if receipt is not None:
        verify_receipt(receipt, plan)
        # A late retry of an earlier restore is read-only even after a subsequent
        # restore. Never rotate new shares or bring a superseded generation back.
        expected = {item.id: new_epoch(plan, item.id) for item in plan.shares}
        active = len(current) == len(expected) and all(
            item.id in expected
            and item.generation == plan.generation
            and item.epoch == expected[item.id]
            for item in current
        )
        return {
            "generation": str(plan.generation),
            "share_count": receipt.share_count,
            "plan_sha256": fingerprint,
            "state": "already-applied" if active else "superseded",
        }
    if current != plan.shares:
        raise RestoreError("Share inventory or generations changed since restore review")
    if any(item.generation == plan.generation for item in current):
        raise RestoreError("Restore generation is already present without this receipt")
    for item in current:
        db.execute(
            update(m.Share)
            .where(m.Share.id == item.id)
            .values(
                collaboration_restore_generation=plan.generation,
                collaboration_epoch=new_epoch(plan, item.id),
            )
        )
        db.add(
            m.AuditLog(
                action=m.AuditAction.SHARE_UPDATED,
                target_share_id=item.id,
                details={
                    "operation": "collaboration_restore",
                    "generation": str(plan.generation),
                    "snapshot_sha256": plan.snapshot_sha256,
                    "target_sha256": plan.target_sha256,
                    "operator": plan.operator,
                },
            )
        )
    db.add(
        m.CollaborationRestore(
            generation=plan.generation,
            plan_sha256=fingerprint,
            snapshot_sha256=plan.snapshot_sha256,
            target_sha256=plan.target_sha256,
            previous_state_sha256=checksum([item.model_dump(mode="json") for item in current]),
            share_count=len(current),
            schema_versions=list(schema_versions),
            operator=plan.operator,
        )
    )
    db.flush()
    return {
        "generation": str(plan.generation),
        "share_count": len(current),
        "plan_sha256": fingerprint,
        "state": "applied",
    }

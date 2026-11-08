"""Authoritative path policy and inventory; never replicate the legacy folder Y.Doc.

Policy is default-deny for members. The longest matching path wins, with an
exact-file rule winning over a folder rule at the same path. Owners/admins
retain management access. Membership removal overrides every stored rule.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import jwt
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models
from app.schemas.token import RelayTokenRequest, RelayTokenResponse
from app.services import collaboration_restore, share_service


def canonical_path(value: str, root: bool = False) -> str:
    # Reject aliases instead of silently normalizing security-sensitive paths.
    if value == "" and root:
        return value
    if (
        not value
        or len(value) > 512
        or value != unicodedata.normalize("NFC", value)
        or any(ord(c) < 32 for c in value)
        or "\\" in value
        or ":" in value
        or any(p in {"", ".", ".."} or p.startswith(".") for p in value.split("/"))
    ):
        raise HTTPException(400, "Use a canonical, relative file path without hidden segments")
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    version: Literal[0] = 0
    type: Literal["folder", "markdown", "canvas", "image", "pdf", "audio", "video", "file"]
    mimetype: str | None = Field(default=None, max_length=255)
    hash: str | None = Field(default=None, max_length=128)
    synctime: int | None = None

    def wire(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


class Change(BaseModel):
    path: str
    previous: Record | None
    value: Record | None

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return canonical_path(value)


class Batch(BaseModel):
    changes: list[Change] = Field(default_factory=list, max_length=1000)
    activity: dict[str, str] = Field(default_factory=dict, max_length=1000)


def member(db: Session, share: models.Share, user: models.User | None) -> None:
    if not share.managed or not user or not user.is_active:
        raise HTTPException(403, "Permission-controlled share access denied")
    if user.is_admin or share.owner_user_id == user.id:
        return
    if share_service._get_member(db, share.id, user.id) is None:
        raise HTTPException(403, "Share membership required")


def rules_for(db: Session, share: models.Share, user: models.User) -> list[models.ManagedRule]:
    return list(
        db.scalars(
            select(models.ManagedRule).where(
                models.ManagedRule.share_id == share.id,
                models.ManagedRule.user_id == user.id,
            )
        )
    )


def permission(
    share: models.Share, user: models.User, path: str, rules: list[models.ManagedRule]
) -> str:
    if user.is_admin or share.owner_user_id == user.id:
        return "write"
    matching = [
        r
        for r in rules
        if r.path == path or (r.recursive and (r.path == "" or path.startswith(r.path + "/")))
    ]
    if not matching:
        return "none"
    return max(matching, key=lambda r: (len(r.path), not r.recursive)).permission


def authorize_entry(
    db: Session, share: models.Share, user: models.User, entry_id: str, write: bool = False
) -> models.ManagedEntry:
    member(db, share, user)
    entry = db.get(models.ManagedEntry, entry_id)
    if not entry or entry.deleted or entry.share_id != share.id:
        raise HTTPException(403, "File access denied")
    allowed = permission(share, user, entry.path, rules_for(db, share, user))
    if allowed == "none" or (write and allowed != "write"):
        raise HTTPException(403, "File access denied")
    return entry


def manifest(db: Session, share: models.Share, user: models.User) -> dict:
    share, user = lock_inventory(db, share, user, read=True)
    rules = rules_for(db, share, user)
    entries = list(
        db.scalars(
            select(models.ManagedEntry).where(
                models.ManagedEntry.share_id == share.id,
                models.ManagedEntry.deleted.is_(False),
            )
        )
    )
    visible = [e for e in entries if permission(share, user, e.path, rules) != "none"]
    from app.services.base_content import manifest_versions

    removed = list(
        db.scalars(
            select(models.ManagedEntry).where(
                models.ManagedEntry.share_id == share.id,
                models.ManagedEntry.deleted.is_(True),
            )
        )
    )
    visible_removed = [
        entry
        for entry in removed
        if entry.last_path and permission(share, user, entry.last_path, rules) != "none"
    ]
    result = {
        "restore_guard": True,
        "restore_generation": (
            str(share.collaboration_restore_generation)
            if share.collaboration_restore_generation
            else None
        ),
        "records": {e.path: e.record for e in visible},
        "permissions": {e.path: permission(share, user, e.path, rules) for e in visible},
        "activity": {e.id: e.activity for e in visible if e.activity},
        "owner": user.is_admin or share.owner_user_id == user.id,
        "base_content": manifest_versions(db, share.id, visible, visible_removed),
        "deleted": {
            path: [e.id for e in visible_removed if e.last_path == path]
            for path in {e.last_path for e in visible_removed}
        },
    }
    db.commit()
    return result


def invalidate_discussion_snapshots(db: Session, share_id: uuid.UUID) -> None:
    """Caller holds the share write lock; rotate without rewriting stale ORM fields.

    A cursor may have skipped threads whose dependent records were hidden. A
    new epoch forces clients to discard their snapshot when authority changes,
    including on regrant. Ordinary content edits must not trigger this reset.
    """
    db.execute(
        update(models.Share)
        .where(models.Share.id == share_id)
        .values(collaboration_epoch=uuid.uuid4())
    )


def lock_inventory(db: Session, share: models.Share, user: models.User, *, read=False):
    """Refresh authority after the inventory/permission serialization boundary.

    Selecting only Share.id leaves a preloaded restore generation stale after
    waiting for a restore. Readers also hold this lock while assembling a manifest.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Inventory entry points require a session without pending writes")
    share_id, user_id = share.id, user.id
    db.expire_all()
    share = db.scalar(
        select(models.Share)
        .where(models.Share.id == share_id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise HTTPException(404, "Share not found")
    user = db.get(models.User, user_id, populate_existing=True)
    member(db, share, user)
    return share, user


def apply_batch(
    db: Session,
    share: models.Share,
    user: models.User,
    batch: Batch,
    restore_generation: uuid.UUID | None = None,
) -> dict:
    share, user = lock_inventory(db, share, user)
    collaboration_restore.require_generation(share, restore_generation)
    rules = list(
        db.scalars(
            select(models.ManagedRule)
            .where(models.ManagedRule.share_id == share.id, models.ManagedRule.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    )
    entries = list(
        db.scalars(
            select(models.ManagedEntry)
            .where(
                models.ManagedEntry.share_id == share.id,
                models.ManagedEntry.deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    )
    by_path = {e.path: e for e in entries}
    by_id = {e.id: e for e in entries}
    changes = {c.path: c for c in batch.changes}
    if len(changes) != len(batch.changes):
        raise HTTPException(400, "Duplicate paths in a change batch")
    final = {e.path: e.record for e in entries}
    for path, change in changes.items():
        if change.value:
            final[path] = change.value.wire()
        else:
            final.pop(path, None)
    folded = [path.casefold() for path in final]
    if len(set(folded)) != len(folded):
        raise HTTPException(409, "Paths must also be unique on case-insensitive devices")
    for path in final:
        parts = path.split("/")
        for n in range(1, len(parts)):
            parent = final.get("/".join(parts[:n]))
            if parent and parent["type"] != "folder":
                raise HTTPException(409, "A file cannot contain other files")
    for path, change in changes.items():
        if permission(share, user, path, rules) != "write":
            raise HTTPException(403, "File access denied")
        existing = by_path.get(path)
        previous = change.previous.wire() if change.previous else None
        value = change.value.wire() if change.value else None
        actual = existing.record if existing else None
        # An absent source is not an acknowledged delete when the same stable
        # identity now lives elsewhere. Only an exact retry of that completed
        # move may use the idempotency exception below.
        if actual is None and previous and value is None:
            moved = by_id.get(previous["id"])
            if moved:
                destination = changes.get(moved.path)
                if (
                    not destination
                    or not destination.value
                    or destination.value.wire() != moved.record
                ):
                    raise HTTPException(
                        409, "The file inventory changed; preserve local edits and refresh"
                    )
        # Retrying an acknowledged-but-lost response is safe and idempotent.
        if actual != previous and actual != value:
            raise HTTPException(409, "The file inventory changed; preserve local edits and refresh")
        if existing and value and existing.id != value["id"]:
            raise HTTPException(409, "An existing file's identity cannot be replaced")
        if (
            existing
            and value
            and (existing.record["type"] == "folder") != (value["type"] == "folder")
        ):
            raise HTTPException(409, "A file cannot be converted to or from a folder")
        if existing and existing.record["type"] == "folder" and value != actual:
            for child in entries:
                if child.path.startswith(path + "/"):
                    if (
                        permission(share, user, child.path, rules) != "write"
                        or child.path not in changes
                    ):
                        raise HTTPException(
                            403, "A folder operation requires access to every child"
                        )
        if value:
            old = by_id.get(value["id"])
            if old and old.path != path:
                removal = changes.get(old.path)
                if not removal or removal.value is not None:
                    raise HTTPException(409, "Moving a file requires removing its old path")
                if permission(share, user, old.path, rules) != "write":
                    raise HTTPException(403, "File access denied")
            reserved = db.get(models.RelayDocBinding, value["id"])
            if (reserved and reserved.share_id != share.id) or db.get(
                models.Share, uuid.UUID(value["id"])
            ):
                raise HTTPException(409, "File identity is already reserved")
            historic = db.get(models.ManagedEntry, value["id"])
            if historic and historic.deleted:
                raise HTTPException(409, "A deleted file identity cannot be reused")
    from app.services.base_content import guard_inventory

    guard_inventory(db, by_id, changes)
    if any(
        path in by_path
        and (change.value is None or change.value.type != by_path[path].record["type"])
        for path, change in changes.items()
    ):
        # A move removes the old path in this same batch. Deletes and type
        # changes can likewise hide existing targets; additions/hash updates
        # cannot change the authority of an existing target.
        invalidate_discussion_snapshots(db, share.id)
    # Explicit file/folder exceptions travel with a renamed object. Otherwise
    # moving Budget.md under a broadly readable parent would silently unhide it.
    moves = [
        (by_id[str(c.value.id)].path, path, by_id[str(c.value.id)].record["type"] == "folder")
        for path, c in changes.items()
        if c.value and str(c.value.id) in by_id and by_id[str(c.value.id)].path != path
    ]
    comment_rules_moved = False
    for rule_model in (models.ManagedRule, models.ManagedCommentRule):
        all_rules = list(db.scalars(select(rule_model).where(rule_model.share_id == share.id)))
        final_rule_keys = set()
        for rule in all_rules:
            matches = [
                (old, new)
                for old, new, folder in moves
                if rule.path == old or (folder and rule.path.startswith(old + "/"))
            ]
            if matches:
                old, new = max(matches, key=lambda pair: len(pair[0]))
                rule.path = new + rule.path[len(old) :]
                comment_rules_moved |= rule_model is models.ManagedCommentRule
            key = (rule.user_id, rule.path, rule.recursive)
            if key in final_rule_keys:
                raise HTTPException(409, "Moving this path would conflict with an access rule")
            final_rule_keys.add(key)
    if comment_rules_moved:
        db.execute(
            update(models.Share)
            .where(models.Share.id == share.id)
            .values(
                comment_rules_revision=models.Share.comment_rules_revision + 1,
            )
        )
    # Remove first, so a validated rename can reuse its destination atomically.
    moved_ids = {str(c.value.id) for c in changes.values() if c.value}
    for path, change in changes.items():
        if change.value is None and path in by_path:
            entry = by_path[path]
            entry.last_path = path
            entry.path = ".deleted/" + entry.id
            entry.deleted = entry.id not in moved_ids
    db.flush()
    for path, change in changes.items():
        if change.value is None:
            continue
        value = change.value.wire()
        entry = by_id.get(value["id"])
        if entry:
            if entry.record["type"] != value["type"]:
                # A type conversion starts a new content lineage. Otherwise an
                # old note/image/Bases target could become valid again after a
                # conversion away and back. Exact batch retries keep this ID.
                entry.collaboration_generation = uuid.uuid4()
            entry.path, entry.record = path, value
        else:
            db.add(
                models.ManagedEntry(
                    id=value["id"], share_id=share.id, path=path, record=value, deleted=False
                )
            )
            if not db.get(models.RelayDocBinding, value["id"]):
                db.add(models.RelayDocBinding(doc_id=value["id"], share_id=share.id))
    db.flush()
    for entry_id, activity in batch.activity.items():
        if len(activity) > 128:
            raise HTTPException(400, "Invalid activity marker")
        entry = authorize_entry(db, share, user, entry_id, write=True)
        entry.activity = activity
    db.commit()
    return manifest(db, share, user)


def grant(
    db: Session, request: Request, payload: RelayTokenRequest, user: models.User | None
) -> RelayTokenResponse:
    share = share_service.get_share(db, payload.share_id)
    member(db, share, user)
    is_index = payload.doc_id == str(share.id)
    if not is_index:
        authorize_entry(db, share, user, payload.doc_id, payload.mode.value == "write")
    settings = get_settings()
    expires = security.utcnow() + timedelta(minutes=5)
    claims = {
        "sub": str(user.id),
        "scope": "managed",
        "share_id": str(share.id),
        "doc_id": payload.doc_id,
        "mode": payload.mode.value,
        "exp": expires,
    }
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        original = security.decode_access_token(header.split(" ", 1)[1])
        if original.get("session_id"):
            claims["session_id"] = original["session_id"]
    base = settings.control_plane_public_url.rstrip("/")
    if is_index:
        endpoint = base + "/v1/managed/index"
    else:
        parsed = urlsplit(base)
        endpoint = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path + "/v1/managed/socket",
                "",
                "",
            )
        )
    return RelayTokenResponse(
        relay_url=endpoint,
        token=jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm),
        expires_at=expires,
    )


def authenticate_grant(
    db: Session, token: str, doc_id: str
) -> tuple[models.Share, models.User, dict]:
    try:
        claims = security.decode_access_token(token)
        if claims.get("scope") != "managed" or claims.get("doc_id") != doc_id:
            raise ValueError()
        share = db.get(models.Share, uuid.UUID(claims["share_id"]))
        user = db.get(models.User, uuid.UUID(claims["sub"]))
        if not share:
            raise ValueError()
        if claims.get("session_id"):
            session = db.get(models.UserSession, uuid.UUID(claims["session_id"]))
            if not session or session.user_id != user.id:
                raise ValueError()
            expiry = session.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= security.utcnow():
                raise ValueError()
    except (jwt.InvalidTokenError, ValueError, KeyError, AttributeError):
        raise HTTPException(401, "Invalid or expired managed grant") from None
    member(db, share, user)
    return share, user, claims

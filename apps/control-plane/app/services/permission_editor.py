"""Atomic owner/admin editing of independent file and comment rules.

The revision covers both actual rule sets and the existing policy epoch. Legacy
writers need no new version counter, and cannot invisibly invalidate this form.
The unchanged legacy APIs remain independent for backward compatibility.
"""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import models as m
from app.schemas.collaboration import (
    CommentRule,
    PermissionFileRule,
    PermissionMember,
    PermissionPolicy,
    PermissionPolicyUpdate,
)
from app.services import managed_service, share_service


def _lock(db: Session, share: m.Share, actor: m.User, write: bool) -> m.Share:
    managed_service.member(db, share, actor)
    share_service.ensure_owner_or_admin(actor, share)
    current = db.scalar(
        select(m.Share)
        .where(m.Share.id == share.id)
        .with_for_update(read=not write)
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise HTTPException(404, "Share not found")
    managed_service.member(db, current, actor)
    share_service.ensure_owner_or_admin(actor, current)
    return current


def _key(rule: PermissionFileRule | CommentRule) -> tuple[str, str, bool]:
    return str(rule.user_id), rule.path, rule.recursive


def _wire(rules: list[PermissionFileRule] | list[CommentRule]) -> list[dict]:
    return [r.model_dump(mode="json") for r in sorted(rules, key=_key)]


def _snapshot(db: Session, share: m.Share, actor: m.User) -> PermissionPolicy:
    file_rules = [
        PermissionFileRule.model_validate(r)
        for r in db.scalars(
            select(m.ManagedRule)
            .where(m.ManagedRule.share_id == share.id)
            .execution_options(populate_existing=True)
        )
    ]
    comment_rules = [
        CommentRule.model_validate(r)
        for r in db.scalars(
            select(m.ManagedCommentRule)
            .where(m.ManagedCommentRule.share_id == share.id)
            .execution_options(populate_existing=True)
        )
    ]
    material = {
        "version": 1,
        "share": str(share.id),
        "owner": str(share.owner_user_id),
        "epoch": str(share.collaboration_epoch),
        "comment_revision": share.comment_rules_revision,
        "files": _wire(file_rules),
        "comments": _wire(comment_rules),
    }
    revision = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf8")
    ).hexdigest()
    # These are display/selection hints. Membership and active-user state are
    # checked again for changed rules under the save transaction below.
    members = db.execute(
        select(m.User.id, m.User.email, m.User.is_active, m.User.is_admin)
        .join(m.ShareMember, m.ShareMember.user_id == m.User.id)
        .where(m.ShareMember.share_id == share.id)
        .order_by(m.User.email, m.User.id)
    )
    return PermissionPolicy(
        share_id=share.id,
        actor_id=actor.id,
        owner_id=share.owner_user_id,
        revision=revision,
        comment_revision=share.comment_rules_revision,
        file_rules=sorted(file_rules, key=_key),
        comment_rules=sorted(comment_rules, key=_key),
        members=[
            PermissionMember(
                user_id=row.id, email=row.email, active=row.is_active, administrator=row.is_admin
            )
            for row in members
        ],
    )


def get_policy(db: Session, share: m.Share, actor: m.User, actor_id: uuid.UUID) -> PermissionPolicy:
    if actor.id != actor_id:
        raise HTTPException(403, "permission_editor_account_changed")
    return _snapshot(db, _lock(db, share, actor, False), actor)


def replace_policy(
    db: Session, share: m.Share, actor: m.User, payload: PermissionPolicyUpdate
) -> PermissionPolicy:
    if payload.actor_id != actor.id:
        raise HTTPException(403, "permission_editor_account_changed")
    share = _lock(db, share, actor, True)
    current = _snapshot(db, share, actor)
    if payload.expected_revision != current.revision:
        raise HTTPException(409, "permission_policy_changed")

    changed_users: set[uuid.UUID] = set()
    for proposed, original in (
        (payload.file_rules, current.file_rules),
        (payload.comment_rules, current.comment_rules),
    ):
        old = {_key(rule): rule for rule in original}
        seen: set[tuple[str, str, bool]] = set()
        for rule in proposed:
            managed_service.canonical_path(rule.path, root=rule.recursive)
            key = _key(rule)
            if key in seen:
                raise HTTPException(400, "duplicate_permission_rule")
            seen.add(key)
            if old.get(key) != rule:
                changed_users.add(rule.user_id)

    if changed_users:
        # Lock membership rows after the parent share: concurrent removal can
        # happen before or after this save, never halfway through its validation.
        members = set(
            db.scalars(
                select(m.ShareMember.user_id)
                .where(m.ShareMember.share_id == share.id, m.ShareMember.user_id.in_(changed_users))
                .order_by(m.ShareMember.user_id)
                .with_for_update(read=True)
            )
        )
        users = list(
            db.scalars(
                select(m.User)
                .where(m.User.id.in_(changed_users))
                .execution_options(populate_existing=True)
            )
        )
        eligible = {
            u.id for u in users if u.is_active and not u.is_admin and u.id != share.owner_user_id
        }
        if not changed_users.issubset(members & eligible):
            raise HTTPException(409, "permission_member_changed")

    files_changed = _wire(payload.file_rules) != _wire(current.file_rules)
    comments_changed = _wire(payload.comment_rules) != _wire(current.comment_rules)
    if not files_changed and not comments_changed:
        return current
    if files_changed:
        db.execute(delete(m.ManagedRule).where(m.ManagedRule.share_id == share.id))
        db.add_all(
            m.ManagedRule(share_id=share.id, **rule.model_dump()) for rule in payload.file_rules
        )
    if comments_changed:
        db.execute(delete(m.ManagedCommentRule).where(m.ManagedCommentRule.share_id == share.id))
        db.add_all(
            m.ManagedCommentRule(share_id=share.id, **rule.model_dump())
            for rule in payload.comment_rules
        )
        share.comment_rules_revision += 1
    managed_service.invalidate_discussion_snapshots(db, share.id)
    db.add(
        m.AuditLog(
            action=m.AuditAction.SHARE_UPDATED,
            actor_user_id=actor.id,
            target_share_id=share.id,
            details={
                "change": "permission_policy",
                "files_changed": files_changed,
                "comments_changed": comments_changed,
                "comment_revision": share.comment_rules_revision,
            },
        )
    )
    db.flush()
    result = _snapshot(db, share, actor)
    db.commit()
    return result

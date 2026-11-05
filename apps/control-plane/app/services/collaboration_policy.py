"""Separate comment grants. Never use these capabilities to issue a Yjs write token."""

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import models
from app.schemas.collaboration import (
    CommentRule,
    CommentRuleSet,
    CommentRuleUpdate,
    EntryCapabilities,
)
from app.services import managed_service, share_service


def rules_for(
    db: Session, share: models.Share, user: models.User
) -> list[models.ManagedCommentRule]:
    return list(
        db.scalars(
            select(models.ManagedCommentRule).where(
                models.ManagedCommentRule.share_id == share.id,
                models.ManagedCommentRule.user_id == user.id,
            )
        )
    )


def can_comment(
    share: models.Share,
    user: models.User,
    path: str,
    file_permission: str,
    rules: list[models.ManagedCommentRule],
) -> bool:
    """Pure precedence calculation; caller must first check active membership/read access."""
    if file_permission not in {"read", "write"}:
        return False
    if user.is_admin or share.owner_user_id == user.id:
        return True
    matching = [
        r
        for r in rules
        if r.path == path or (r.recursive and (r.path == "" or path.startswith(r.path + "/")))
    ]
    if not matching:
        return file_permission == "write"
    return max(matching, key=lambda r: (len(r.path), not r.recursive)).allowed


def entry_capabilities(
    db: Session, share: models.Share, user: models.User, entry_id: str
) -> EntryCapabilities:
    entry = managed_service.authorize_entry(db, share, user, entry_id)
    file_permission = managed_service.permission(
        share, user, entry.path, managed_service.rules_for(db, share, user)
    )
    return EntryCapabilities(
        read=True,
        comment=can_comment(share, user, entry.path, file_permission, rules_for(db, share, user)),
        edit=file_permission == "write",
        moderate=user.is_admin or share.owner_user_id == user.id,
    )


def authorize_comment(db: Session, share: models.Share, user: models.User, entry_id: str) -> None:
    if not entry_capabilities(db, share, user, entry_id).comment:
        raise HTTPException(403, "Comment access denied")


def get_rules(db: Session, share: models.Share, user: models.User) -> CommentRuleSet:
    managed_service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    # Keep revision and rows from the same policy state. A separate SELECT of
    # each at READ COMMITTED could pair old revision metadata with new rules.
    share = db.scalar(
        select(models.Share)
        .where(models.Share.id == share.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise HTTPException(404, "Share not found")
    managed_service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    rules = db.scalars(
        select(models.ManagedCommentRule)
        .where(
            models.ManagedCommentRule.share_id == share.id,
        )
        .order_by(
            models.ManagedCommentRule.user_id,
            models.ManagedCommentRule.path,
            models.ManagedCommentRule.recursive,
        )
    )
    return CommentRuleSet(
        revision=share.comment_rules_revision, rules=[CommentRule.model_validate(r) for r in rules]
    )


def replace_rules(
    db: Session, share: models.Share, user: models.User, update: CommentRuleUpdate
) -> CommentRuleSet:
    managed_service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    # Same serialization lock as managed inventory/rule updates. Refresh the
    # ORM identity after waiting; otherwise the revision may predate the lock.
    share = db.scalar(
        select(models.Share)
        .where(models.Share.id == share.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise HTTPException(404, "Share not found")
    managed_service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    if update.expected_revision != share.comment_rules_revision:
        raise HTTPException(409, "Comment rules changed; reload before saving")
    seen = set()
    for rule in update.rules:
        managed_service.canonical_path(rule.path, root=rule.recursive)
        key = (rule.user_id, rule.path, rule.recursive)
        if key in seen or not db.get(models.User, rule.user_id):
            raise HTTPException(400, "Duplicate rule or missing user")
        seen.add(key)
    db.execute(
        delete(models.ManagedCommentRule).where(
            models.ManagedCommentRule.share_id == share.id,
        )
    )
    for rule in update.rules:
        db.add(models.ManagedCommentRule(share_id=share.id, **rule.model_dump()))
    share.comment_rules_revision += 1
    db.add(
        models.AuditLog(
            action=models.AuditAction.SHARE_UPDATED,
            actor_user_id=user.id,
            target_share_id=share.id,
            details={"change": "comment_rules", "revision": share.comment_rules_revision},
        )
    )
    result = get_rules(db, share, user)
    db.commit()
    return result

import uuid

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core import security
from app.core.config import get_settings
from app.db import models
from app.schemas.collaboration import CommentRuleUpdate
from app.services import collaboration_policy as policy
from app.services import managed_service as managed
from app.services import share_service


def auth(user):
    return {"Authorization": "Bearer " + security.create_access_token(str(user.id))}


@pytest.fixture
def team(db_session, test_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", True)
    reviewer = models.User(email="reviewer@example.com", password_hash="", is_active=True)
    db_session.add(reviewer)
    db_session.flush()
    share = models.Share(
        kind=models.ShareKind.FOLDER,
        path="Docs",
        managed=True,
        visibility=models.ShareVisibility.PRIVATE,
        owner_user_id=test_user.id,
    )
    db_session.add(share)
    db_session.flush()
    db_session.add(
        models.ShareMember(
            share_id=share.id, user_id=reviewer.id, role=models.ShareMemberRole.VIEWER
        )
    )
    db_session.add(
        models.ManagedRule(
            share_id=share.id, user_id=reviewer.id, path="", recursive=True, permission="read"
        )
    )
    entry = models.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=share.id,
        path="Notes/One.md",
        record={"type": "markdown"},
        deleted=False,
    )
    db_session.add(entry)
    db_session.commit()
    return share, test_user, reviewer, entry


def replace(db, share, owner, reviewer, *rules, revision=None):
    return policy.replace_rules(
        db,
        share,
        owner,
        CommentRuleUpdate(
            expected_revision=share.comment_rules_revision if revision is None else revision,
            rules=[dict(user_id=reviewer.id, **r) for r in rules],
        ),
    )


def test_reviewer_comment_grant_does_not_grant_note_write(db_session, team, client):
    share, owner, reviewer, entry = team
    assert not policy.entry_capabilities(db_session, share, reviewer, entry.id).comment
    replace(db_session, share, owner, reviewer, dict(path="Notes", allowed=True))
    capabilities = policy.entry_capabilities(db_session, share, reviewer, entry.id)
    assert capabilities.model_dump() == dict(read=True, comment=True, edit=False, moderate=False)
    with pytest.raises(HTTPException) as error:
        managed.authorize_entry(db_session, share, reviewer, entry.id, write=True)
    assert error.value.status_code == 403
    response = client.post(
        "/v1/tokens/relay",
        headers=auth(reviewer),
        json={
            "share_id": str(share.id),
            "doc_id": entry.id,
            "mode": "write",
        },
    )
    assert response.status_code == 403


def test_rule_replacement_with_production_autoflush_disabled(db_session, team):
    share, owner, reviewer, entry = team
    db_session.autoflush = False
    result = replace(db_session, share, owner, reviewer, dict(path="", allowed=True))
    assert result.revision == 1 and len(result.rules) == 1
    db_session.expire_all()
    assert policy.get_rules(db_session, share, owner) == result
    assert policy.entry_capabilities(db_session, share, reviewer, entry.id).comment


def test_editor_default_exact_denial_and_path_boundary(db_session, team):
    share, owner, reviewer, entry = team
    rule = db_session.scalar(select(models.ManagedRule))
    rule.permission = "write"
    db_session.commit()
    assert policy.entry_capabilities(db_session, share, reviewer, entry.id).comment
    replace(
        db_session,
        share,
        owner,
        reviewer,
        dict(path="", allowed=False),
        dict(path="Notes", allowed=True),
        dict(path=entry.path, recursive=False, allowed=False),
    )
    assert not policy.entry_capabilities(db_session, share, reviewer, entry.id).comment
    rules = policy.rules_for(db_session, share, reviewer)
    assert policy.can_comment(share, reviewer, "Notes/Other.md", "read", rules)
    assert not policy.can_comment(share, reviewer, "Notes2/Other.md", "write", rules)
    assert not policy.can_comment(share, reviewer, "Notes/Other.md", "none", rules)
    assert policy.entry_capabilities(db_session, share, owner, entry.id).moderate


def test_read_revocation_membership_and_deletion_override_comment_rule(db_session, team):
    share, owner, reviewer, entry = team
    replace(db_session, share, owner, reviewer, dict(path="", allowed=True))
    rule = db_session.scalar(select(models.ManagedRule))
    rule.permission = "none"
    db_session.commit()
    with pytest.raises(HTTPException):
        policy.authorize_comment(db_session, share, reviewer, entry.id)
    rule.permission = "read"
    entry.deleted = True
    db_session.commit()
    with pytest.raises(HTTPException):
        policy.authorize_comment(db_session, share, reviewer, entry.id)
    entry.deleted = False
    membership = share_service._get_member(db_session, share.id, reviewer.id)
    db_session.delete(membership)
    db_session.commit()
    with pytest.raises(HTTPException):
        policy.authorize_comment(db_session, share, reviewer, entry.id)


def test_rules_are_owner_only_versioned_and_survive_legacy_put(client, db_session, team):
    share, owner, reviewer, entry = team
    url = f"/v1/collaboration/shares/{share.id}/comment-rules"
    payload = {
        "expected_revision": 0,
        "rules": [dict(user_id=str(reviewer.id), path="", allowed=True)],
    }
    assert client.get(url, headers=auth(reviewer)).status_code == 403
    assert client.put(url, headers=auth(reviewer), json=payload).status_code == 403
    result = client.put(url, headers=auth(owner), json=payload)
    assert result.status_code == 200
    assert result.json()["revision"] == 1
    assert client.put(url, headers=auth(owner), json=payload).status_code == 409
    legacy = f"/v1/managed/shares/{share.id}/rules"
    assert (
        client.put(
            legacy,
            headers=auth(owner),
            json=[
                dict(
                    user_id=str(reviewer.id),
                    path="",
                    permission="read",
                )
            ],
        ).status_code
        == 200
    )
    assert client.get(url, headers=auth(owner)).json() == result.json()
    caps = client.get(
        f"/v1/collaboration/shares/{share.id}/entries/{entry.id}/capabilities",
        headers=auth(reviewer),
    )
    assert caps.json() == dict(read=True, comment=True, edit=False, moderate=False)
    audit = db_session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.target_share_id == share.id,
            models.AuditLog.action == models.AuditAction.SHARE_UPDATED,
        )
    )
    assert audit.details == {"change": "comment_rules", "revision": 1}


@pytest.mark.parametrize(
    "bad",
    [
        [{"path": "../secret", "allowed": True}],
        [{"path": "", "recursive": False, "allowed": True}],
        [{"path": "Notes", "allowed": True}, {"path": "Notes", "allowed": False}],
    ],
)
def test_invalid_rule_replacement_preserves_existing_rules(db_session, team, bad):
    share, owner, reviewer, entry = team
    replace(db_session, share, owner, reviewer, dict(path="", allowed=True))
    with pytest.raises(HTTPException):
        replace(db_session, share, owner, reviewer, *bad)
    db_session.rollback()
    assert policy.get_rules(db_session, share, owner).revision == 1
    assert policy.entry_capabilities(db_session, share, reviewer, entry.id).comment


def test_comment_exceptions_follow_rename_and_invalidate_stale_rule_editor(db_session, team):
    share, owner, reviewer, entry = team
    replace(
        db_session,
        share,
        owner,
        reviewer,
        dict(path="", allowed=True),
        dict(path=entry.path, recursive=False, allowed=False),
    )
    old_revision = share.comment_rules_revision
    record = dict(id=entry.id, version=0, type="markdown")
    entry.record = record
    db_session.commit()
    managed.apply_batch(
        db_session,
        share,
        owner,
        managed.Batch(
            changes=[
                dict(path=entry.path, previous=record, value=None),
                dict(path="Moved.md", previous=None, value=record),
            ]
        ),
    )
    assert not policy.entry_capabilities(db_session, share, reviewer, entry.id).comment
    assert policy.get_rules(db_session, share, owner).revision == old_revision + 1
    with pytest.raises(HTTPException) as error:
        replace(
            db_session, share, owner, reviewer, dict(path="", allowed=True), revision=old_revision
        )
    assert error.value.status_code == 409


def test_preview_disabled_and_no_unimplemented_feature_advertised(client, team, monkeypatch):
    share, owner, reviewer, entry = team
    url = f"/v1/collaboration/capabilities?share_id={share.id}"
    features = client.get(url, headers=auth(reviewer)).json()["features"]
    assert features == dict(
        presence_locations_preview=False,
        comment_permissions=True,
        comments=False,
        bases=False,
        discussion_commands=False,
        discussion_text_anchors=False,
        discussion_block_anchors=False,
        discussion_image_occurrences=False,
        discussion_image_files=False,
    )
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", False)
    assert (
        client.get(url, headers=auth(reviewer)).json()["features"]["comment_permissions"] is False
    )
    assert (
        client.get(
            f"/v1/collaboration/shares/{share.id}/comment-rules", headers=auth(owner)
        ).status_code
        == 404
    )


def test_unshared_entry_and_scoped_tokens_do_not_bypass_auth(client, db_session, team):
    share, owner, reviewer, entry = team
    other = models.Share(
        kind=models.ShareKind.FOLDER,
        path="Private",
        managed=True,
        visibility=models.ShareVisibility.PRIVATE,
        owner_user_id=owner.id,
    )
    db_session.add(other)
    db_session.commit()
    url = f"/v1/collaboration/shares/{other.id}/entries/{entry.id}/capabilities"
    assert client.get(url, headers=auth(reviewer)).status_code == 403
    assert client.get(url, headers=auth(owner)).status_code == 403
    claims = security.decode_access_token(security.create_access_token(str(reviewer.id)))
    claims["scope"] = "managed"
    token = jwt.encode(claims, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm)
    assert (
        client.get(
            f"/v1/collaboration/capabilities?share_id={share.id}",
            headers={"Authorization": "Bearer " + token},
        ).status_code
        == 401
    )

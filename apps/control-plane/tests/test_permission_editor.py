import copy
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_collaboration_policy import auth
from test_collaboration_policy import team as policy_team

from app.core.config import get_settings
from app.db import models as m
from app.schemas.collaboration import PermissionPolicyUpdate
from app.services import collaboration_policy, managed_service, permission_editor

team = policy_team


def endpoint(team):
    return f"/v1/collaboration/shares/{team[0].id}/permissions"


def read(client, team, user=None, expected=200):
    user = user or team[1]
    response = client.get(endpoint(team), params={"actor_id": str(user.id)}, headers=auth(user))
    assert response.status_code == expected, response.text
    return response.json()


def update(snapshot, **changes):
    return (
        dict(
            version=1,
            actor_id=snapshot["actor_id"],
            expected_revision=snapshot["revision"],
            file_rules=copy.deepcopy(snapshot["file_rules"]),
            comment_rules=copy.deepcopy(snapshot["comment_rules"]),
        )
        | changes
    )


def save(client, team, payload, user=None, expected=200):
    response = client.put(endpoint(team), json=payload, headers=auth(user or team[1]))
    assert response.status_code == expected, response.text
    return response.json()


def commenter(team, **fields):
    return dict(user_id=str(team[2].id), path="", recursive=True, allowed=True) | fields


def test_atomic_reviewer_preset_grants_read_and_comment_but_never_write(client, db_session, team):
    share, owner, reviewer, entry = team
    legacy_file = f"/v1/managed/shares/{share.id}/rules"
    # Start hidden; the combined operation must establish both capabilities.
    assert client.put(legacy_file, json=[], headers=auth(owner)).status_code == 200
    before = read(client, team)
    assert before["file_rules"] == before["comment_rules"] == []
    result = save(
        client,
        team,
        update(
            before,
            file_rules=[dict(user_id=str(reviewer.id), path="", recursive=True, permission="read")],
            comment_rules=[commenter(team)],
        ),
    )
    assert result["revision"] != before["revision"]
    assert result["comment_revision"] == 1
    assert read(client, team) == result
    assert result["members"] == [
        dict(user_id=str(reviewer.id), email=reviewer.email, active=True, administrator=False)
    ]
    assert collaboration_policy.entry_capabilities(
        db_session, share, reviewer, entry.id
    ).model_dump() == dict(read=True, comment=True, edit=False, moderate=False)
    with pytest.raises(HTTPException) as error:
        managed_service.authorize_entry(db_session, share, reviewer, entry.id, write=True)
    assert error.value.status_code == 403
    audit = db_session.scalar(select(m.AuditLog).where(m.AuditLog.target_share_id == share.id))
    assert audit.details == dict(
        change="permission_policy", files_changed=True, comments_changed=True, comment_revision=1
    )


def test_explicit_comment_denial_can_restrict_an_editor_and_never_overrides_hidden_files(
    client, db_session, team
):
    before = read(client, team)
    payload = update(before, comment_rules=[commenter(team, allowed=False)])
    payload["file_rules"][0]["permission"] = "write"
    after = save(client, team, payload)
    share, _, reviewer, entry = team
    assert collaboration_policy.entry_capabilities(
        db_session, share, reviewer, entry.id
    ).model_dump() == dict(read=True, comment=False, edit=True, moderate=False)
    payload = update(after, file_rules=[], comment_rules=[commenter(team)])
    save(client, team, payload)
    with pytest.raises(HTTPException):
        collaboration_policy.entry_capabilities(db_session, share, reviewer, entry.id)


def test_only_current_owner_or_admin_can_read_or_save_and_actor_switches_are_rejected(
    client, db_session, team
):
    before = read(client, team)
    read(client, team, team[2], expected=403)
    save(client, team, update(before, actor_id=str(team[2].id)), team[2], expected=403)
    admin = m.User(
        email="permission-admin@example.com", password_hash="", is_active=True, is_admin=True
    )
    db_session.add(admin)
    db_session.commit()
    snapshot = read(client, team, admin)
    assert snapshot["actor_id"] == str(admin.id)
    # A token refresh returning a different privileged account must not silently
    # turn an existing form into that account's administrative action.
    save(client, team, update(before, comment_rules=[commenter(team)]), admin, expected=403)
    response = client.get(endpoint(team), params={"actor_id": str(team[1].id)}, headers=auth(admin))
    assert response.status_code == 403
    save(client, team, update(snapshot, comment_rules=[commenter(team)]), admin)


def test_policy_feature_gate_and_managed_share_requirement(client, db_session, team, monkeypatch):
    before = read(client, team)
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", False)
    read(client, team, expected=404)
    save(client, team, update(before), expected=404)
    monkeypatch.setattr(get_settings(), "collaboration_policy_enabled", True)
    team[0].managed = False
    db_session.commit()
    read(client, team, expected=403)
    save(client, team, update(before), expected=403)


@pytest.mark.parametrize("writer", ["files", "comments", "epoch"])
def test_legacy_writers_and_policy_epoch_changes_invalidate_old_combined_forms(
    client, db_session, team, writer
):
    before = read(client, team)
    if writer == "files":
        rules = copy.deepcopy(before["file_rules"])
        rules[0]["permission"] = "write"
        response = client.put(
            f"/v1/managed/shares/{team[0].id}/rules", headers=auth(team[1]), json=rules
        )
        assert response.status_code == 200
    elif writer == "comments":
        response = client.put(
            f"/v1/collaboration/shares/{team[0].id}/comment-rules",
            headers=auth(team[1]),
            json=dict(expected_revision=0, rules=[commenter(team)]),
        )
        assert response.status_code == 200
    else:
        managed_service.invalidate_discussion_snapshots(db_session, team[0].id)
        db_session.commit()
    changed = read(client, team)
    assert changed["revision"] != before["revision"]
    assert (
        save(
            client,
            team,
            update(before, comment_rules=[commenter(team, allowed=False)]),
            expected=409,
        )["error"]["message"]
        == "permission_policy_changed"
    )
    assert read(client, team) == changed


@pytest.mark.parametrize(
    "invalid", ["duplicate", "traversal", "root-exact", "unknown-member", "inactive", "owner"]
)
def test_invalid_combined_updates_leave_both_rule_sets_and_epoch_unchanged(
    client, db_session, team, invalid
):
    if invalid == "inactive":
        team[2].is_active = False
        db_session.commit()
    before = read(client, team)
    payload = update(before, file_rules=[], comment_rules=[commenter(team)])
    if invalid == "duplicate":
        payload["comment_rules"] *= 2
    if invalid == "traversal":
        payload["comment_rules"][0]["path"] = "../Notes"
    if invalid == "root-exact":
        payload["comment_rules"][0]["recursive"] = False
    if invalid == "unknown-member":
        payload["comment_rules"][0]["user_id"] = str(uuid.uuid4())
    if invalid == "owner":
        payload["comment_rules"][0]["user_id"] = str(team[1].id)
    expected = 400 if invalid in {"duplicate", "traversal", "root-exact"} else 409
    save(client, team, payload, expected=expected)
    assert read(client, team) == before
    assert (
        list(db_session.scalars(select(m.AuditLog).where(m.AuditLog.target_share_id == team[0].id)))
        == []
    )


def test_removed_members_existing_rules_can_be_kept_or_removed_but_not_expanded(
    client, db_session, team
):
    first = save(client, team, update(read(client, team), comment_rules=[commenter(team)]))
    member = db_session.scalar(select(m.ShareMember).where(m.ShareMember.share_id == team[0].id))
    db_session.delete(member)
    db_session.commit()
    current = read(client, team)
    assert current["members"] == []
    assert save(client, team, update(current)) == current
    save(client, team, update(current, comment_rules=[commenter(team, path="New")]), expected=409)
    result = save(client, team, update(current, file_rules=[], comment_rules=[]))
    assert result["file_rules"] == result["comment_rules"] == []
    assert result["comment_revision"] == first["comment_revision"] + 1


def test_unchanged_save_is_a_noop_and_file_only_save_preserves_legacy_comment_revision(
    client, db_session, team
):
    before = read(client, team)
    assert save(client, team, update(before)) == before
    assert (
        list(db_session.scalars(select(m.AuditLog).where(m.AuditLog.target_share_id == team[0].id)))
        == []
    )
    payload = update(before)
    payload["file_rules"][0]["permission"] = "write"
    result = save(client, team, payload)
    assert result["comment_revision"] == before["comment_revision"]
    assert result["revision"] != before["revision"]
    assert result["comment_rules"] == before["comment_rules"]


def test_combined_transaction_rolls_back_file_rules_comment_rules_epoch_and_audit(
    db_session, team, monkeypatch
):
    share, owner, _, _ = team
    db_session.autoflush = False
    original = permission_editor.get_policy(db_session, share, owner, owner.id)
    before = original.model_dump(mode="json")
    payload = PermissionPolicyUpdate.model_validate(
        update(before, file_rules=[], comment_rules=[commenter(team)])
    )

    def failed_commit():
        raise RuntimeError("Synthetic commit failure")

    with monkeypatch.context() as changes:
        changes.setattr(db_session, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="Synthetic"):
            permission_editor.replace_policy(db_session, share, owner, payload)
    db_session.rollback()
    assert permission_editor.get_policy(db_session, share, owner, owner.id) == original
    assert (
        list(db_session.scalars(select(m.AuditLog).where(m.AuditLog.target_share_id == team[0].id)))
        == []
    )


def test_strict_bounded_wire_validation(client, team):
    snapshot = read(client, team)
    for field, value in [("recursive", "yes"), ("allowed", 1)]:
        save(
            client,
            team,
            update(snapshot, comment_rules=[commenter(team, **{field: value})]),
            expected=422,
        )
    save(client, team, update(snapshot, comment_rules=[commenter(team)] * 1001), expected=422)
    save(client, team, update(snapshot, expected_revision="unversioned"), expected=422)
    save(client, team, update(snapshot, invented_field=True), expected=422)


@pytest.mark.parametrize("changed", [False, True])
def test_restore_guard_precedes_stale_revision_and_noop_checks(client, db_session, team, changed):
    before = read(client, team)
    assert before["restore_guard"] is True and before["restore_generation"] is None
    generation = uuid.uuid4()
    team[0].collaboration_restore_generation = generation
    db_session.commit()
    restored = read(client, team)
    assert restored["restore_generation"] == str(generation)
    assert restored["revision"] != before["revision"]
    for snapshot in [before, restored]:
        payload = update(snapshot, comment_rules=[commenter(team)] if changed else [])
        for old in [None, str(uuid.uuid4())]:
            headers = auth(team[1])
            if old:
                headers["X-Relay-Restore-Generation"] = old
            response = client.put(endpoint(team), json=payload, headers=headers)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["message"] == {"code": "restore_generation_changed"}
            assert read(client, team) == restored
    assert (
        list(db_session.scalars(select(m.AuditLog).where(m.AuditLog.target_share_id == team[0].id)))
        == []
    )
    headers = auth(team[1]) | {"X-Relay-Restore-Generation": str(generation)}
    response = client.put(endpoint(team), json=update(before), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "permission_policy_changed"
    response = client.put(
        endpoint(team),
        json=update(restored, comment_rules=[commenter(team)] if changed else []),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["restore_generation"] == str(generation)
    assert response.json()["comment_rules"] == ([commenter(team)] if changed else [])


def test_permission_restore_header_is_strict_and_does_not_allow_generation_downgrade(client, team):
    before = read(client, team)
    for generation, expected in [("not-a-uuid", 422), (str(uuid.uuid4()), 409)]:
        response = client.put(
            endpoint(team),
            json=update(before),
            headers=auth(team[1]) | {"X-Relay-Restore-Generation": generation},
        )
        assert response.status_code == expected, response.text
        assert read(client, team) == before

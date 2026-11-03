import uuid

import pytest
from fastapi import HTTPException

from app.core import security
from app.db import models
from app.services import managed_service as managed
from app.services import share_service


@pytest.fixture
def team(db_session, test_user):
    bob = models.User(email="bob@example.com", password_hash="", is_active=True, is_admin=False)
    db_session.add(bob)
    db_session.flush()
    share = models.Share(
        kind=models.ShareKind.FOLDER,
        path="Projects",
        visibility=models.ShareVisibility.PRIVATE,
        owner_user_id=test_user.id,
        managed=True,
    )
    db_session.add(share)
    db_session.flush()
    db_session.add(
        models.ShareMember(share_id=share.id, user_id=bob.id, role=models.ShareMemberRole.EDITOR)
    )
    db_session.commit()
    return share, test_user, bob


def add_rule(db, share, user, path, permission, recursive=True):
    db.add(
        models.ManagedRule(
            share_id=share.id,
            user_id=user.id,
            path=path,
            recursive=recursive,
            permission=permission,
        )
    )
    db.commit()


def record(kind="markdown"):
    return {"id": str(uuid.uuid4()), "version": 0, "type": kind}


def change(path, value, previous=None):
    return {"path": path, "previous": previous, "value": value}


def apply(db, share, user, *changes):
    return managed.apply_batch(db, share, user, managed.Batch(changes=list(changes)))


def test_hidden_child_not_in_inventory_or_grants(db_session, team):
    share, owner, bob = team
    public, secret = record(), record()
    apply(db_session, share, owner, change("Notes.md", public), change("Budget.md", secret))
    assert managed.manifest(db_session, share, bob)["records"] == {}
    add_rule(db_session, share, bob, "", "read")
    add_rule(db_session, share, bob, "Budget.md", "none", False)
    visible = managed.manifest(db_session, share, bob)
    assert set(visible["records"]) == {"Notes.md"}
    assert "Budget" not in str(visible)
    assert secret["id"] not in str(visible)
    managed.authorize_entry(db_session, share, bob, public["id"])
    for entry_id, write in ((secret["id"], False), (public["id"], True)):
        with pytest.raises(HTTPException):
            managed.authorize_entry(db_session, share, bob, entry_id, write)


def test_file_exception_and_folder_boundary(db_session, team):
    share, owner, bob = team
    add_rule(db_session, share, bob, "Projects", "write")
    add_rule(db_session, share, bob, "Projects/Budget.md", "none", False)
    rules = managed.rules_for(db_session, share, bob)
    assert managed.permission(share, bob, "Projects/Note.md", rules) == "write"
    assert managed.permission(share, bob, "Projects/Budget.md", rules) == "none"
    assert managed.permission(share, bob, "Projects2/Note.md", rules) == "none"
    add_rule(db_session, share, bob, "Elsewhere/One.md", "write", False)
    apply(db_session, share, bob, change("Elsewhere/One.md", record()))
    with pytest.raises(HTTPException):
        apply(db_session, share, bob, change("Elsewhere/Two.md", record()))


@pytest.mark.parametrize(
    "path",
    [
        "/a.md",
        "../a.md",
        "a/../b.md",
        "a//b.md",
        "a\\b.md",
        "a/./b.md",
        ".obsidian/config",
        "a\x00.md",
        "C:a.md",
        "e\u0301.md",
    ],
)
def test_path_aliases_rejected(path):
    with pytest.raises(HTTPException):
        managed.canonical_path(path)


def test_rename_checks_both_paths_and_preserves_identity(db_session, team):
    share, owner, bob = team
    note = record()
    apply(db_session, share, owner, change("Allowed/Note.md", note))
    add_rule(db_session, share, bob, "Allowed", "write")
    with pytest.raises(HTTPException):
        apply(
            db_session,
            share,
            bob,
            change("Allowed/Note.md", None, note),
            change("Hidden/Note.md", note),
        )
    db_session.rollback()
    apply(
        db_session,
        share,
        bob,
        change("Allowed/Note.md", None, note),
        change("Allowed/Renamed.md", note),
    )
    assert managed.authorize_entry(db_session, share, bob, note["id"]).path == "Allowed/Renamed.md"


def test_folder_delete_cannot_erase_hidden_child(db_session, team):
    share, owner, bob = team
    folder, hidden = record("folder"), record()
    apply(db_session, share, owner, change("Docs", folder), change("Docs/Hidden.md", hidden))
    add_rule(db_session, share, bob, "Docs", "write")
    add_rule(db_session, share, bob, "Docs/Hidden.md", "none", False)
    with pytest.raises(HTTPException):
        apply(db_session, share, bob, change("Docs", None, folder))


def test_retry_is_idempotent_stale_edit_and_identity_reuse_rejected(db_session, team):
    share, owner, _ = team
    note = record()
    apply(db_session, share, owner, change("Note.md", note))
    apply(db_session, share, owner, change("Note.md", note))
    with pytest.raises(HTTPException):
        apply(db_session, share, owner, change("Note.md", record()))
    apply(db_session, share, owner, change("Note.md", None, note))
    with pytest.raises(HTTPException):
        apply(db_session, share, owner, change("Note.md", note))


def test_membership_revocation_overrides_policy_and_live_grant(db_session, team):
    share, owner, bob = team
    note = record()
    apply(db_session, share, owner, change("Note.md", note))
    add_rule(db_session, share, bob, "", "write")
    managed.authorize_entry(db_session, share, bob, note["id"], True)
    membership = share_service._get_member(db_session, share.id, bob.id)
    db_session.delete(membership)
    db_session.commit()
    with pytest.raises(HTTPException):
        managed.authorize_entry(db_session, share, bob, note["id"], True)


def test_explicit_hidden_file_rule_moves_with_the_file(db_session, team):
    share, owner, bob = team
    note = record()
    apply(db_session, share, owner, change("Budget.md", note))
    add_rule(db_session, share, bob, "", "read")
    add_rule(db_session, share, bob, "Budget.md", "none", False)
    apply(db_session, share, owner, change("Budget.md", None, note), change("Renamed.md", note))
    assert managed.manifest(db_session, share, bob)["records"] == {}
    with pytest.raises(HTTPException):
        managed.authorize_entry(db_session, share, bob, note["id"])


def auth(user):
    return {"Authorization": "Bearer " + security.create_access_token(str(user.id))}


def test_legacy_routes_and_cross_share_token_do_not_bypass_policy(client, db_session, team):
    share, owner, bob = team
    note = record()
    apply(db_session, share, owner, change("Secret.md", note))
    for path in ("files-index", "download?path=Secret.md"):
        assert client.get(f"/v1/shares/{share.id}/{path}", headers=auth(bob)).status_code == 403
    result = client.post(
        "/v1/tokens/relay",
        headers=auth(bob),
        json={"share_id": str(share.id), "doc_id": note["id"], "mode": "read"},
    )
    assert result.status_code == 403
    result = client.post(
        "/v1/tokens/relay",
        headers=auth(bob),
        json={"share_id": str(share.id), "doc_id": str(share.id), "mode": "write"},
    )
    assert result.status_code == 200
    grant = result.json()["token"]
    assert result.json()["relay_url"].endswith("/v1/managed/index")
    assert client.get("/v1/shares", headers={"Authorization": "Bearer " + grant}).status_code == 401
    index = client.get(
        f"/v1/managed/index/{share.id}", headers={"Authorization": "Bearer " + grant}
    )
    assert index.status_code == 200
    assert "Secret" not in index.text
    # Existing members cannot read blobs by guessing a stable file id either.
    response = client.post(
        f"/v1/shares/{share.id}/file-token",
        headers=auth(bob),
        json={
            "path": note["id"],
            "sha256": "a" * 64,
            "content_type": "text/plain",
            "content_length": 1,
        },
    )
    assert response.status_code == 403


def test_rules_owner_only_and_managed_shares_stay_private(client, team):
    share, owner, bob = team
    rules_url = f"/v1/managed/shares/{share.id}/rules"
    assert client.get(rules_url, headers=auth(bob)).status_code == 403
    rules = [{"user_id": str(bob.id), "path": "", "recursive": True, "permission": "read"}]
    assert client.put(rules_url, headers=auth(owner), json=rules).status_code == 200
    assert (
        client.patch(
            f"/v1/shares/{share.id}", headers=auth(owner), json={"visibility": "public"}
        ).status_code
        == 400
    )
    assert client.get("/v1/shares", headers=auth(owner)).json()[0]["managed"] is True


def test_deletion_is_distinct_from_revocation_and_hides_private_tombstones(db_session, team):
    share, owner, bob = team
    public, private = record(), record()
    apply(db_session, share, owner, change("Public.md", public), change("Private.md", private))
    add_rule(db_session, share, bob, "Public.md", "read", False)
    apply(
        db_session,
        share,
        owner,
        change("Public.md", None, public),
        change("Private.md", None, private),
    )
    snapshot = managed.manifest(db_session, share, bob)
    assert snapshot["deleted"] == {"Public.md": [public["id"]]}
    assert "Private" not in str(snapshot)
    assert private["id"] not in str(snapshot)
    replacement = record()
    apply(db_session, share, owner, change("Public.md", replacement))
    snapshot = managed.manifest(db_session, share, bob)
    assert snapshot["records"]["Public.md"] == replacement
    assert replacement["id"] not in snapshot["deleted"]["Public.md"]


@pytest.mark.parametrize("expired", [False, True])
def test_session_revocation_invalidates_api_and_managed_grants(client, db_session, team, expired):
    from datetime import timedelta

    from app.services import session_service

    share, owner, _ = team
    session, _ = session_service.create_session(db_session, owner.id)
    db_session.commit()
    token = security.create_access_token(str(owner.id), session_id=str(session.id))
    headers = {"Authorization": "Bearer " + token}
    response = client.post(
        "/v1/tokens/relay",
        headers=headers,
        json={"share_id": str(share.id), "doc_id": str(share.id), "mode": "write"},
    )
    assert response.status_code == 200
    grant = response.json()["token"]
    if expired:
        session.expires_at = security.utcnow() - timedelta(seconds=1)
    else:
        db_session.delete(session)
    db_session.commit()
    assert client.get("/v1/shares", headers=headers).status_code == 401
    assert (
        client.get(
            f"/v1/managed/index/{share.id}", headers={"Authorization": "Bearer " + grant}
        ).status_code
        == 401
    )


def test_path_probe_does_not_disclose_hidden_names_or_existence(client, db_session, team):
    share, owner, bob = team
    apply(db_session, share, owner, change("Private.md", record()))
    add_rule(db_session, share, bob, "Work", "write")
    token = client.post(
        "/v1/tokens/relay",
        headers=auth(bob),
        json={"share_id": str(share.id), "doc_id": str(share.id), "mode": "write"},
    ).json()["token"]
    response = client.post(
        f"/v1/managed/index/{share.id}/access",
        headers={"Authorization": "Bearer " + token},
        json={"paths": ["Work/New.md", "Unknown.md"]},
    )
    assert response.json() == {"Work/New.md": "write", "Unknown.md": "none"}
    assert "Private" not in response.text


def test_claim_preserves_canonical_identity_and_returns_effective_access(client, db_session, team):
    share, owner, bob = team

    def grant(user):
        return client.post(
            "/v1/tokens/relay",
            headers=auth(user),
            json={
                "share_id": str(share.id),
                "doc_id": str(share.id),
                "mode": "write",
            },
        ).json()["token"]

    route = f"/v1/managed/index/{share.id}/claim"
    owner_headers = {"Authorization": "Bearer " + grant(owner)}
    first = record()
    result = client.post(route, headers=owner_headers, json={"path": "New.md", "record": first})
    assert result.json() == {"won": True, "record": first, "permission": "write"}
    result = client.post(route, headers=owner_headers, json={"path": "New.md", "record": record()})
    assert result.json() == {"won": False, "record": first, "permission": "write"}
    bob_headers = {"Authorization": "Bearer " + grant(bob)}
    assert (
        client.post(
            route, headers=bob_headers, json={"path": "New.md", "record": record()}
        ).status_code
        == 403
    )
    add_rule(db_session, share, bob, "New.md", "read", False)
    result = client.post(route, headers=bob_headers, json={"path": "New.md", "record": record()})
    assert result.json() == {"won": False, "record": first, "permission": "read"}


def test_stale_delete_after_remote_move_is_not_an_acknowledged_delete(db_session, team):
    share, owner, _ = team
    note = record()
    apply(db_session, share, owner, change("Old.md", note))
    move = [change("Old.md", None, note), change("New.md", note)]
    apply(db_session, share, owner, *move)
    # Repeating the exact successful move remains idempotent.
    apply(db_session, share, owner, *move)
    with pytest.raises(HTTPException) as exc:
        apply(db_session, share, owner, change("Old.md", None, note))
    assert exc.value.status_code == 409
    assert managed.manifest(db_session, share, owner)["records"] == {"New.md": note}
    apply(db_session, share, owner, change("New.md", None, note))
    # A genuinely completed deletion is still safe to retry.
    apply(db_session, share, owner, change("New.md", None, note))

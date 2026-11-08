import uuid

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect
from test_discussions import headers
from test_discussions import world as discussion_world

from app.api.routers import managed
from app.api.routers.managed import check_socket
from app.core import security
from app.db import models as m
from app.services import managed_service

world = discussion_world


def mint(client, w, *, binding=None, mode="write", index=False, actor="owner", expected=200):
    auth = headers(w.tokens[actor])
    if binding is not None:
        auth["X-Relay-Restore-Generation"] = str(binding)
    response = client.post(
        "/v1/tokens/relay",
        headers=auth,
        json={
            "share_id": str(w.share.id),
            "doc_id": str(w.share.id) if index else w.entry.id,
            "mode": mode,
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["read", "write"])
def test_document_mint_and_socket_checks_retain_original_lineage(client, world, db_session, mode):
    original = mint(client, world, mode=mode)
    assert original["restore_guard"] is True and original["restore_generation"] is None
    assert check_socket(original["token"], world.entry.id)[:2] == (str(world.share.id), mode)
    world.share.collaboration_restore_generation = restored = uuid.uuid4()
    db_session.commit()
    with pytest.raises(HTTPException) as refused:
        check_socket(original["token"], world.entry.id)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": "restore_generation_changed"}
    mint(client, world, mode=mode, expected=409)
    mint(client, world, mode=mode, binding=uuid.uuid4(), expected=409)
    current = mint(client, world, mode=mode, binding=restored)
    assert current["restore_generation"] == str(restored)
    assert security.decode_access_token(current["token"])["restore_generation"] == str(restored)
    assert check_socket(current["token"], world.entry.id)[:2] == (str(world.share.id), mode)


def test_index_grants_can_discover_restore_without_silently_adopting_it(client, world, db_session):
    world.share.collaboration_restore_generation = uuid.uuid4()
    db_session.commit()
    data = mint(client, world, index=True)
    assert data["restore_generation"] == str(world.share.collaboration_restore_generation)
    assert security.decode_access_token(data["token"])["restore_generation"] is None
    response = client.get(f"/v1/managed/index/{world.share.id}", headers=headers(data["token"]))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["restore_generation"] == data["restore_generation"]
    mint(client, world, index=True, binding=uuid.uuid4(), expected=409)
    mint(client, world, binding="bad", expected=422)


def test_legacy_original_grant_is_valid_only_before_restore_and_malformed_binding_is_rejected(
    client, world, db_session
):
    data = mint(client, world)
    claims = security.decode_access_token(data["token"])
    del claims["restore_generation"]
    # Sign only synthetic fixture claims, using the same helper as the issuer.
    import jwt

    from app.core.config import get_settings

    def signed():
        settings = get_settings()
        return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    assert check_socket(signed(), world.entry.id)[1] == "write"
    world.share.collaboration_restore_generation = uuid.uuid4()
    db_session.commit()
    with pytest.raises(HTTPException) as restored:
        check_socket(signed(), world.entry.id)
    assert restored.value.status_code == 409
    claims["restore_generation"] = 17
    with pytest.raises(HTTPException) as malformed:
        check_socket(signed(), world.entry.id)
    assert malformed.value.status_code == 401


def test_mint_rechecks_session_after_authority_lock_wait(client, world, db_session, monkeypatch):
    lock = managed_service.lock_inventory

    def after_wait(db, share, user, **kwargs):
        result = lock(db, share, user, **kwargs)
        db.delete(db.get(m.UserSession, world.sessions["owner"]))
        db.commit()
        return result

    monkeypatch.setattr(managed_service, "lock_inventory", after_wait)
    mint(client, world, expected=401)


def test_bound_socket_still_requires_current_file_permission_and_live_session(
    client, world, db_session
):
    world.share.collaboration_restore_generation = uuid.uuid4()
    db_session.commit()
    grant = mint(
        client,
        world,
        actor="reader",
        mode="read",
        binding=world.share.collaboration_restore_generation,
    )
    assert check_socket(grant["token"], world.entry.id)[1] == "read"
    rule = db_session.query(m.ManagedRule).filter_by(user_id=world.reader.id).one()
    rule.permission = "none"
    db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        check_socket(grant["token"], world.entry.id)
    assert revoked.value.status_code == 403
    rule.permission = "read"
    db_session.delete(db_session.get(m.UserSession, world.sessions["reader"]))
    db_session.commit()
    with pytest.raises(HTTPException) as logged_out:
        check_socket(grant["token"], world.entry.id)
    assert logged_out.value.status_code == 401


@pytest.mark.parametrize("revoked", [False, True])
def test_stale_socket_delivers_restore_close_only_to_currently_authorized_readers(
    client, world, db_session, monkeypatch, revoked
):
    grant = mint(client, world, mode="read", actor="reader")
    world.share.collaboration_restore_generation = uuid.uuid4()
    if revoked:
        db_session.query(m.ManagedRule).filter_by(user_id=world.reader.id).one().permission = "none"
    db_session.commit()

    def forbidden_upstream(*args, **kwargs):
        pytest.fail("An obsolete grant must never open an upstream relay connection")

    monkeypatch.setattr(managed, "connect", forbidden_upstream)
    url = f"/v1/managed/socket/{world.entry.id}?token={grant['token']}"
    if revoked:
        with pytest.raises(WebSocketDisconnect) as denied, client.websocket_connect(url):
            pytest.fail("Revoked readers must not be admitted")
        assert denied.value.code == 4403
    else:
        with client.websocket_connect(url) as socket:
            assert socket.receive() == {"type": "websocket.close", "code": 4412, "reason": ""}

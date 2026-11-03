from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routers import native_oauth as native
from app.core import security
from app.db import models
from app.schemas.oauth import OAuthUserInfo
from app.services import oauth_service

VERIFIER = "a" * 43
DISCOVERY = {
    "issuer": "https://id.example.com",
    "authorization_endpoint": "https://id.example.com/authorize",
    "token_endpoint": "https://id.example.com/api/oidc/token",
    "userinfo_endpoint": "https://id.example.com/api/oidc/userinfo",
    "jwks_uri": "https://id.example.com/.well-known/jwks.json",
}


@pytest.fixture
def provider(db_session, monkeypatch):
    provider = models.OAuthProvider(
        name="pocket-id",
        provider_type=models.OAuthProviderType.OIDC,
        issuer_url=DISCOVERY["issuer"],
        client_id="client",
        client_secret_encrypted="secret",
        enabled=True,
        auto_register=True,
    )
    db_session.add(provider)
    db_session.commit()
    monkeypatch.setattr(
        oauth_service, "_discover_oidc_config", AsyncMock(return_value=DISCOVERY.copy())
    )
    return provider


def start(client):
    response = client.post(
        "/v1/auth/native/start",
        json={
            "provider": "pocket-id",
            "challenge": oauth_service.generate_code_challenge(VERIFIER),
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    params = parse_qs(urlsplit(data["authorize_url"]).query)
    return data, params


def test_handoff_bound_to_client_single_use_and_no_tokens_in_browser(client, provider, monkeypatch):
    data, params = start(client)
    assert data["authorize_url"].startswith(DISCOVERY["authorization_endpoint"] + "?")
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"]
    assert "secret" not in data["authorize_url"]
    claim = {"id": data["id"], "verifier": VERIFIER}
    assert client.post("/v1/auth/native/complete", json=claim).status_code == 202
    assert (
        client.post("/v1/auth/native/complete", json={**claim, "verifier": "b" * 43}).status_code
        == 400
    )
    monkeypatch.setattr(
        native,
        "authenticate_identity",
        AsyncMock(
            return_value=OAuthUserInfo(
                sub="alice",
                email="alice@example.com",
                email_verified=True,
            )
        ),
    )
    callback = client.get(
        "/v1/auth/native/callback", params={"state": params["state"][0], "code": "code"}
    )
    assert callback.status_code == 200
    assert "Return to Obsidian" in callback.text
    assert "access_token" not in callback.text
    assert not callback.cookies
    assert (
        client.get(
            "/v1/auth/native/callback", params={"state": params["state"][0], "code": "code"}
        ).status_code
        == 400
    )
    result = client.post("/v1/auth/native/complete", json=claim)
    assert result.status_code == 200, result.text
    assert result.json()["refresh_token"]
    assert (
        security.decode_access_token(result.json()["access_token"])["sub"]
        == result.json()["user_id"]
    )
    assert client.post("/v1/auth/native/complete", json=claim).status_code == 401


def test_cancellation_and_expiry(client, provider, db_session):
    data, params = start(client)
    claim = {"id": data["id"], "verifier": VERIFIER}
    assert client.post("/v1/auth/native/cancel", json=claim).status_code == 204
    assert (
        client.get(
            "/v1/auth/native/callback", params={"state": params["state"][0], "code": "code"}
        ).status_code
        == 400
    )
    data, _ = start(client)
    attempt = db_session.scalar(
        select(models.NativeLogin).where(models.NativeLogin.status == "pending")
    )
    attempt.expires_at = security.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert (
        client.post(
            "/v1/auth/native/complete", json={"id": data["id"], "verifier": VERIFIER}
        ).status_code
        == 400
    )


def test_identity_never_automatically_links_email(provider, test_user, db_session):
    with pytest.raises(HTTPException) as denied:
        native.resolve_user(
            db_session,
            provider,
            OAuthUserInfo(sub="attacker", email=test_user.email, email_verified=True),
        )
    assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_discovery_checks_issuer_and_transport(provider, monkeypatch):
    for changed in (
        {"issuer": "https://different.example"},
        {"token_endpoint": "http://unsafe.example/token"},
    ):
        monkeypatch.setattr(
            oauth_service, "_discover_oidc_config", AsyncMock(return_value={**DISCOVERY, **changed})
        )
        with pytest.raises(HTTPException):
            await native.discover(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims,subject",
    [
        ({"sub": "alice", "exp": 9999999999, "nonce": "wrong"}, "alice"),
        ({"sub": "alice", "exp": 9999999999, "nonce": "nonce"}, "bob"),
        ({"sub": "alice", "nonce": "nonce"}, "alice"),
    ],
)
async def test_identity_claim_mismatch_fails_closed(provider, monkeypatch, claims, subject):
    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = Response({"id_token": "signed", "access_token": "opaque"})
    client.get.return_value = Response({"sub": subject, "email": "alice@example.com"})
    monkeypatch.setattr(native.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(oauth_service, "validate_id_token", AsyncMock(return_value=claims))
    attempt = models.NativeLogin(
        nonce="nonce", code_verifier=VERIFIER, redirect_uri="https://cp.example/callback"
    )
    with pytest.raises(HTTPException):
        await native.authenticate_identity(provider, attempt, "code")

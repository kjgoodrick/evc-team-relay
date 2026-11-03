"""OIDC browser handoff: the browser never receives an application credential.

The public start/complete endpoints need no existing login. A random verifier
kept by the initiating plugin binds completion to that installation. Database
compare-and-swap transitions make callback and completion single-use across workers.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.core.http import get_client_ip
from app.db import models
from app.db.session import get_db
from app.schemas.oauth import OAuthCallbackResponse, OAuthUserInfo
from app.services import audit_service, oauth_service, session_service

router = APIRouter(prefix="/auth/native", tags=["oauth"])
limiter = Limiter(key_func=get_client_ip)
TTL_SECONDS = 300
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class StartRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class CompleteRequest(BaseModel):
    id: uuid.UUID
    verifier: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")


def _safe_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise HTTPException(502, "OIDC endpoints must use HTTPS")
    return value


async def discover(provider: models.OAuthProvider) -> dict:
    _safe_endpoint(provider.issuer_url)
    config = await oauth_service._discover_oidc_config(provider.issuer_url)
    if config.get("issuer") != provider.issuer_url:
        raise HTTPException(502, "OIDC discovery issuer does not match configuration")
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri"):
        if not isinstance(config.get(key), str):
            raise HTTPException(502, "Incomplete OIDC discovery document")
        _safe_endpoint(config[key])
    return config


@router.post("/start")
@limiter.limit("10/minute")
async def start(payload: StartRequest, request: Request, db: Session = Depends(get_db)):
    provider = oauth_service.get_oauth_provider(db, payload.provider)
    config = await discover(provider)
    settings = get_settings()
    callback = _safe_endpoint(
        settings.control_plane_public_url.rstrip("/") + "/v1/auth/native/callback"
    )
    state = secrets.token_urlsafe(32)
    attempt = models.NativeLogin(
        provider_id=provider.id,
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        client_challenge=payload.challenge,
        code_verifier=oauth_service.generate_code_verifier(),
        nonce=secrets.token_urlsafe(32),
        redirect_uri=callback,
        expires_at=security.utcnow() + timedelta(seconds=TTL_SECONDS),
    )
    # Bounded retention; never retain completed login credentials or IdP tokens.
    db.execute(delete(models.NativeLogin).where(models.NativeLogin.expires_at < security.utcnow()))
    db.add(attempt)
    db.commit()
    params = {
        "client_id": provider.client_id,
        "response_type": "code",
        "redirect_uri": callback,
        "scope": settings.oauth_scopes,
        "state": state,
        "nonce": attempt.nonce,
        "code_challenge": oauth_service.generate_code_challenge(attempt.code_verifier),
        "code_challenge_method": "S256",
    }
    return JSONResponse(
        {
            "id": str(attempt.id),
            "expires_in": TTL_SECONDS,
            "interval": 2,
            "authorize_url": config["authorization_endpoint"] + "?" + urlencode(params),
        },
        headers=NO_STORE,
    )


async def authenticate_identity(
    provider: models.OAuthProvider, attempt: models.NativeLogin, code: str
) -> OAuthUserInfo:
    config = await discover(provider)
    # A confidential web client is registered at the IdP. Its secret never
    # leaves this backend, even when the caller is a mobile Obsidian plugin.
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            config["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": provider.client_id,
                "client_secret": oauth_service.get_client_secret(provider),
                "code": code,
                "redirect_uri": attempt.redirect_uri,
                "code_verifier": attempt.code_verifier,
            },
        )
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get("id_token") or not tokens.get("access_token"):
            raise HTTPException(401, "The provider did not return OIDC credentials")
        claims = await oauth_service.validate_id_token(provider, tokens["id_token"])
        if (
            not claims.get("sub")
            or not claims.get("exp")
            or not hmac.compare_digest(str(claims.get("nonce", "")), attempt.nonce)
            or (
                isinstance(claims.get("aud"), list)
                and len(claims["aud"]) > 1
                and claims.get("azp") != provider.client_id
            )
        ):
            raise HTTPException(401, "Invalid OIDC identity claims")
        response = await client.get(
            config["userinfo_endpoint"],
            headers={
                "Authorization": "Bearer " + tokens["access_token"],
            },
        )
        response.raise_for_status()
        profile = response.json()
    if profile.get("sub") != claims["sub"]:
        raise HTTPException(401, "OIDC userinfo subject does not match ID token")
    # Use the signed identity for email ownership, not userinfo's assertion.
    email = claims.get("email") or profile.get("email")
    if not email or (claims.get("email") and profile.get("email") != claims["email"]):
        raise HTTPException(401, "OIDC email is missing or inconsistent")
    return OAuthUserInfo(
        sub=claims["sub"],
        email=email,
        name=profile.get("name"),
        picture=profile.get("picture"),
        groups=[str(g) for g in profile.get("groups", [])]
        if isinstance(profile.get("groups"), list)
        else [],
        email_verified=claims.get("email_verified") is True,
    )


def resolve_user(
    db: Session, provider: models.OAuthProvider, identity: OAuthUserInfo
) -> models.User:
    user = oauth_service.find_user_by_oauth(db, provider.id, identity.sub)
    if not user:
        # No automatic email linking, even for verified emails: a provider's
        # subject is the identity. Existing users must be linked by an admin.
        if oauth_service.find_user_by_email(db, identity.email):
            raise HTTPException(
                409, "An account with this email exists; ask an administrator to link this identity"
            )
        if not provider.auto_register:
            raise HTTPException(403, "Auto-registration is disabled")
        user = oauth_service.create_user_from_oauth(
            db,
            email=identity.email,
            name=identity.name,
            provider_id=provider.id,
            provider_user_id=identity.sub,
            picture_url=identity.picture,
            groups=identity.groups,
        )
    if not user.is_active:
        raise HTTPException(403, "User account is inactive")
    return user


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    state: str,
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(models.NativeLogin).where(
            models.NativeLogin.state_hash == hashlib.sha256(state.encode()).hexdigest()
        )
    )
    if attempt is None:
        raise HTTPException(400, "Invalid or expired login")
    changed = db.execute(
        update(models.NativeLogin)
        .execution_options(synchronize_session=False)
        .where(
            models.NativeLogin.id == attempt.id,
            models.NativeLogin.status == "pending",
            models.NativeLogin.expires_at > security.utcnow(),
        )
        .values(status="processing")
    )
    db.commit()
    if changed.rowcount != 1:
        raise HTTPException(400, "Invalid or expired login")
    try:
        if error or not code:
            raise HTTPException(401, "Sign-in was not completed")
        provider = db.get(models.OAuthProvider, attempt.provider_id)
        if not provider or not provider.enabled:
            raise HTTPException(403, "Provider is disabled")
        identity = await authenticate_identity(provider, attempt, code)
        user = resolve_user(db, provider, identity)
        db.execute(
            update(models.NativeLogin)
            .execution_options(synchronize_session=False)
            .where(
                models.NativeLogin.id == attempt.id,
                models.NativeLogin.status == "processing",
                models.NativeLogin.expires_at > security.utcnow(),
            )
            .values(status="ready", user_id=user.id, code_verifier="")
        )
        db.commit()
    except Exception:
        db.rollback()
        db.execute(
            update(models.NativeLogin)
            .execution_options(synchronize_session=False)
            .where(models.NativeLogin.id == attempt.id)
            .values(status="failed", code_verifier="")
        )
        db.commit()
        raise HTTPException(401, "Sign-in failed. Return to Obsidian and try again.") from None
    return HTMLResponse(
        "<!doctype html><meta name='viewport' content='width=device-width'><title>Signed in</title>"
        "<h1>Signed in</h1><p>Return to Obsidian to finish connecting. "
        "You can close this page.</p>",
        headers={
            **NO_STORE,
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


def _attempt_for_client(db: Session, payload: CompleteRequest) -> models.NativeLogin:
    attempt = db.get(models.NativeLogin, payload.id)
    if not attempt or not hmac.compare_digest(
        attempt.client_challenge, oauth_service.generate_code_challenge(payload.verifier)
    ):
        raise HTTPException(400, "Invalid or expired login")
    expiry = (
        attempt.expires_at.replace(tzinfo=timezone.utc)
        if attempt.expires_at.tzinfo is None
        else attempt.expires_at
    )
    if expiry <= datetime.now(timezone.utc):
        raise HTTPException(400, "Invalid or expired login")
    return attempt


@router.post("/complete")
@limiter.limit("90/minute")
def complete(payload: CompleteRequest, request: Request, db: Session = Depends(get_db)):
    attempt = _attempt_for_client(db, payload)
    if attempt.status in {"pending", "processing"}:
        return JSONResponse({"status": "pending"}, status_code=202, headers=NO_STORE)
    user = db.get(models.User, attempt.user_id) if attempt.user_id else None
    provider = db.get(models.OAuthProvider, attempt.provider_id)
    if (
        attempt.status != "ready"
        or not user
        or not user.is_active
        or not provider
        or not provider.enabled
    ):
        raise HTTPException(401, "Sign-in failed or was already completed")
    changed = db.execute(
        update(models.NativeLogin)
        .execution_options(synchronize_session=False)
        .where(
            models.NativeLogin.id == attempt.id,
            models.NativeLogin.status == "ready",
            models.NativeLogin.expires_at > security.utcnow(),
        )
        .values(status="consumed")
    )
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(401, "Sign-in was already completed")
    settings = get_settings()
    session, refresh = session_service.create_session(
        db,
        user.id,
        device_name="Obsidian SSO",
        user_agent=request.headers.get("user-agent"),
        ip_address=get_client_ip(request),
        expires_days=settings.refresh_token_expire_days,
    )
    audit_service.log_action(
        db=db,
        action=models.AuditAction.OAUTH_LOGIN,
        actor_user_id=user.id,
        details={"provider": provider.name, "flow": "native"},
    )
    db.commit()
    result = OAuthCallbackResponse(
        access_token=security.create_access_token(str(user.id), session_id=str(session.id)),
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user_id=str(user.id),
        user_email=user.email,
    )
    return JSONResponse(result.model_dump(), headers=NO_STORE)


@router.post("/cancel", status_code=204)
def cancel(payload: CompleteRequest, db: Session = Depends(get_db)):
    attempt = _attempt_for_client(db, payload)
    db.execute(
        update(models.NativeLogin)
        .execution_options(synchronize_session=False)
        .where(
            models.NativeLogin.id == attempt.id,
            models.NativeLogin.status != "consumed",
        )
        .values(status="cancelled", code_verifier="")
    )
    db.commit()

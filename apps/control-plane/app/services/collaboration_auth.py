"""Short-lived identity guards shared by collaboration policy and discussion routes."""

import uuid

from fastapi import Depends, HTTPException
from jwt.exceptions import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import deps
from app.core import security
from app.db import models
from app.db.session import get_db


def lock_actor(db: Session, token: str, *, require_session=False) -> models.User:
    user = deps._get_user_from_token(db, token)
    try:
        payload = security.decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(401, "Invalid or expired token") from None
    if require_session and not payload.get("session_id"):
        raise HTTPException(401, "Sign in again with a session-backed credential")
    user = db.scalar(
        select(models.User)
        .where(models.User.id == user.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if not user or not user.is_active:
        raise HTTPException(401, "User is inactive or missing")
    if payload.get("session_id"):
        session = db.scalar(
            select(models.UserSession)
            .where(
                models.UserSession.id == uuid.UUID(payload["session_id"]),
                models.UserSession.user_id == user.id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if session is None:
            raise HTTPException(401, "Session was revoked")
    # Lock waits can outlive the credential's validity.
    deps._get_user_from_token(db, token)
    return user


def get_actor(
    db: Session = Depends(get_db), token: str | None = Depends(deps.oauth2_scheme)
) -> models.User:
    if not token:
        raise HTTPException(401, "Missing credentials")
    return lock_actor(db, token)

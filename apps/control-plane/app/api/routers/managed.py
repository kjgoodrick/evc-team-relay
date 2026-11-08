from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.api import deps
from app.core import security
from app.core.config import get_settings
from app.db import models
from app.db.session import get_db, get_sessionmaker
from app.services import collaboration_restore, share_service
from app.services import managed_service as service
from app.services.presence_protocol import (
    InvalidPresence,
    PresenceConnection,
    PresenceIdentity,
    identity_for,
)

router = APIRouter(prefix="/managed", tags=["managed shares"])


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Managed grant required")
    return authorization.split(" ", 1)[1]


@router.get("/index/{share_id}")
def index(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    share, user, _ = service.authenticate_grant(db, bearer(authorization), str(share_id))
    return service.manifest(db, share, user)


@router.post("/index/{share_id}")
def mutate_index(
    share_id: uuid.UUID,
    payload: service.Batch,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    share, user, claims = service.authenticate_grant(db, bearer(authorization), str(share_id))
    if claims["mode"] != "write":
        raise HTTPException(403, "Read-only grant")
    return service.apply_batch(db, share, user, payload, restore_generation)


class Rule(BaseModel):
    user_id: uuid.UUID
    path: str = Field(max_length=512)
    recursive: bool = True
    permission: str = Field(pattern=r"^(none|read|write)$")


class AccessRequest(BaseModel):
    paths: list[str] = Field(max_length=1000)


@router.post("/index/{share_id}/access")
def path_access(
    share_id: uuid.UUID,
    payload: AccessRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    share, user, _ = service.authenticate_grant(db, bearer(authorization), str(share_id))
    rules = service.rules_for(db, share, user)
    # Answer only about caller-supplied paths, without disclosing rules, file
    # existence or identifiers. This lets adoption skip local private files.
    return {
        path: service.permission(share, user, service.canonical_path(path), rules)
        for path in payload.paths
    }


class Claim(BaseModel):
    path: str
    record: service.Record


@router.post("/index/{share_id}/claim")
def claim_file(
    share_id: uuid.UUID,
    payload: Claim,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    share, user, claims = service.authenticate_grant(db, bearer(authorization), str(share_id))
    path = service.canonical_path(payload.path)
    if claims["mode"] != "write":
        raise HTTPException(403, "Read-only grant")
    share, user = service.lock_inventory(db, share, user)
    collaboration_restore.require_generation(share, restore_generation)
    restore_context = {
        "restore_guard": True,
        "restore_generation": str(restore_generation) if restore_generation else None,
    }
    existing = db.scalar(
        select(models.ManagedEntry).where(
            models.ManagedEntry.share_id == share_id,
            models.ManagedEntry.path == path,
            models.ManagedEntry.deleted.is_(False),
        )
    )
    if existing:
        service.authorize_entry(db, share, user, existing.id)
        return {
            "won": existing.id == str(payload.record.id),
            "record": existing.record,
            "permission": service.permission(share, user, path, service.rules_for(db, share, user)),
            **restore_context,
        }
    service.apply_batch(
        db,
        share,
        user,
        service.Batch(
            changes=[
                service.Change(
                    path=path,
                    previous=None,
                    value=payload.record,
                )
            ]
        ),
        restore_generation,
    )
    return {"won": True, "record": payload.record.wire(), "permission": "write", **restore_context}


@router.get("/shares/{share_id}/rules")
def get_rules(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
):
    share = share_service.get_share(db, share_id)
    service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    return [
        Rule.model_validate(r, from_attributes=True)
        for r in db.scalars(
            select(models.ManagedRule).where(models.ManagedRule.share_id == share_id)
        )
    ]


@router.put("/shares/{share_id}/rules")
def set_rules(
    share_id: uuid.UUID,
    payload: list[Rule],
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
    restore_generation: Annotated[
        uuid.UUID | None, Header(alias="X-Relay-Restore-Generation")
    ] = None,
):
    share = share_service.get_share(db, share_id)
    service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    if len(payload) > 1000:
        raise HTTPException(400, "Too many rules")
    share = db.scalar(
        select(models.Share)
        .where(models.Share.id == share_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if share is None:
        raise HTTPException(404, "Share not found")
    service.member(db, share, user)
    share_service.ensure_owner_or_admin(user, share)
    collaboration_restore.require_generation(share, restore_generation)
    seen = set()
    for rule in payload:
        service.canonical_path(rule.path, root=rule.recursive)
        key = (rule.user_id, rule.path, rule.recursive)
        if key in seen or not db.get(models.User, rule.user_id):
            raise HTTPException(400, "Duplicate rule or missing user")
        seen.add(key)
    db.execute(delete(models.ManagedRule).where(models.ManagedRule.share_id == share_id))
    for rule in payload:
        db.add(models.ManagedRule(share_id=share_id, **rule.model_dump()))
    service.invalidate_discussion_snapshots(db, share_id)
    db.commit()
    return payload


def check_socket(
    token: str, doc_id: str, include_presence: bool = False
) -> tuple[str, str, PresenceIdentity | None]:
    # Fresh session each time: no idle transaction/identity-map cache can hide
    # membership removal, policy changes, account disablement or session revocation.
    with get_sessionmaker()() as db:
        # Refresh authority after any restore/permission lock wait. Check the
        # immutable grant binding; never assign the current generation to it.
        share, user, claims = service.authenticate_grant(db, token, doc_id, read_lock=True)
        try:
            generation = claims.get("restore_generation")
            generation = uuid.UUID(generation) if generation is not None else None
        except (ValueError, TypeError, AttributeError) as error:
            raise HTTPException(401, "Invalid managed grant binding") from error
        collaboration_restore.require_generation(share, generation)
        entry = service.authorize_entry(db, share, user, doc_id, claims["mode"] == "write")
        if entry.record["type"] not in {"markdown", "canvas"}:
            raise HTTPException(403, "Not a collaborative document")
        identity = identity_for(db, user, claims) if include_presence else None
        return str(share.id), claims["mode"], identity


@router.websocket("/socket/{doc_id}")
async def document_socket(websocket: WebSocket, doc_id: str, token: str):
    try:
        share_id, mode, _ = await run_in_threadpool(check_socket, token, doc_id)
    except HTTPException:
        await websocket.close(code=4403)
        return
    settings = get_settings()
    # The client receives a managed JWT, never this relay credential. Thus a
    # previously issued client grant cannot bypass live policy checks by
    # opening the public relay directly or calling its HTTP snapshot API.
    relay_token = security.create_relay_token_cwt(
        private_key=websocket.app.state.relay_private_key,
        key_id=websocket.app.state.relay_key_id,
        doc_id=doc_id,
        mode=mode,
        expires_minutes=5,
        audience=settings.effective_relay_audience,
        issuer=settings.relay_token_issuer,
        share_id=share_id,
    )
    uri = (
        settings.managed_relay_url.rstrip("/")
        + f"/d/{quote(doc_id, safe='')}/ws/{quote(doc_id, safe='')}"
        + f"?token={quote(relay_token, safe='')}"
    )
    await websocket.accept()
    tasks = []
    presence = PresenceConnection()
    close_code = 4403
    try:
        async with connect(uri, max_size=16 * 1024 * 1024, open_timeout=10, proxy=None) as relay:

            async def incoming():
                while True:
                    data = await websocket.receive_bytes()
                    _, _, identity = await run_in_threadpool(
                        check_socket, token, doc_id, bool(data and data[0] == 1)
                    )
                    # The relay enforces read-only Yjs message types too.
                    await relay.send(presence.transform(data, identity))

            async def outgoing():
                async for data in relay:
                    await run_in_threadpool(check_socket, token, doc_id)
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)

            async def revoked():
                while True:
                    await asyncio.sleep(1)
                    await run_in_threadpool(check_socket, token, doc_id)

            tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, revoked)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except (HTTPException, WebSocketDisconnect):
        pass
    except InvalidPresence:
        close_code = 4400
    except ConnectionClosed as error:
        if error.rcvd and error.rcvd.code in {4409, 4429}:
            close_code = error.rcvd.code
    except Exception:
        # No exception text: WebSocket library exceptions can contain the
        # upstream URI, which carries a private relay credential.
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=close_code)

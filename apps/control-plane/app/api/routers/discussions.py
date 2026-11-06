"""Opt-in discussion API; complete collaboration support is not advertised yet."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.api import deps
from app.core.config import get_settings
from app.db.session import get_db
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service
from app.services import discussion_service as service


class BoundedDiscussionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method != "POST":
                return await handler(request)
            body = bytearray()
            limit = 256 * 1024 if anchor_service.enabled() else 65536
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, {"code": "command_too_large"})
                body.extend(chunk)
            if len(body) > 65536:
                try:
                    value = json.loads(body)
                    command = value.get("command") if isinstance(value, dict) else None
                    anchor = command.get("anchor") if isinstance(command, dict) else None
                    is_text = isinstance(anchor, dict) and anchor.get("type") == "text"
                except (ValueError, UnicodeError):
                    is_text = False
                if not is_text:
                    raise HTTPException(413, {"code": "command_too_large"})
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return await request.receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            try:
                return await handler(StarletteRequest(request.scope, receive=receive))
            except RequestValidationError:
                # The application-wide formatter echoes validation inputs.
                # Keep comment bodies out of errors, including invalid Unicode.
                raise HTTPException(422, {"code": "invalid_command"}) from None

        return bounded


def require_preview():
    settings = get_settings()
    if not (settings.collaboration_policy_enabled and settings.collaboration_discussions_enabled):
        raise HTTPException(404, {"code": "discussion_preview_disabled"})


def credential(token: str | None = Depends(deps.oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, {"code": "unauthenticated"})
    return token


router = APIRouter(
    prefix="/collaboration",
    tags=["discussion preview"],
    dependencies=[Depends(require_preview)],
    route_class=BoundedDiscussionRoute,
)


@router.post("/commands")
def command(
    payload: CommandEnvelope, token: str = Depends(credential), db: Session = Depends(get_db)
):
    return service.apply_command(db, token, payload)


@router.get("/shares/{share_id}/entries/{entry_id}/anchor-context")
def anchor_context(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.document_context(db, token, share_id, entry_id)


@router.get("/shares/{share_id}/entries/{entry_id}/threads")
def threads(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.list_threads(db, token, share_id, entry_id, after=after, limit=limit)


@router.get("/shares/{share_id}/entries/{entry_id}/participants")
def participants(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    after: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.participants(db, token, share_id, entry_id, after=after, limit=limit)


@router.get("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}")
def thread(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.read_thread(db, token, share_id, entry_id, thread_id, after=after, limit=limit)


@router.get("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/state")
def thread_state(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.read_thread_state(db, token, share_id, entry_id, thread_id)


@router.get("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/messages/{message_id}")
def message(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.read_message(db, token, share_id, entry_id, thread_id, message_id)


@router.get(
    "/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/messages/{message_id}/history"
)
def message_history(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.history(
        db, token, share_id, entry_id, thread_id, message_id, after=after, limit=limit
    )


@router.get("/shares/{share_id}/entries/{entry_id}/changes")
def changes(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=100),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.changes(db, token, share_id, entry_id, cursor=cursor, limit=limit)

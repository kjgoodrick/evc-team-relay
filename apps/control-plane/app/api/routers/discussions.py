"""Opt-in discussion API; complete collaboration support is not advertised yet."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.api import deps
from app.core.config import get_settings
from app.db.session import get_db
from app.schemas.discussion_moderation import ModerationRead, ModerationReason
from app.schemas.discussion_search import DiscussionSearch
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service, discussion_export, discussion_moderation, discussion_search
from app.services import discussion_service as service


class BoundedDiscussionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method != "POST":
                return await handler(request)
            body = bytearray()
            # Exact accepted retries retain their original body limit even
            # when new group creation has been disabled.
            group_capture = request.url.path.endswith("/bases/group-targets")
            # Recovery must still retire an old text request after creation is
            # disabled. It never validates the anchor through the private relay.
            limit = (
                256 * 1024
                if anchor_service.enabled()
                or group_capture
                or request.url.path.endswith("/commands")
                or request.url.path.endswith("/commands/withdraw")
                or request.url.path.endswith("/commands/status")
                else 65536
            )
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, {"code": "command_too_large"})
                body.extend(chunk)
            if len(body) > 65536:
                try:
                    value = json.loads(body)
                    command = value.get("command") if isinstance(value, dict) else None
                    anchor = command.get("anchor") if isinstance(command, dict) else None
                    is_anchor = isinstance(anchor, dict) and (
                        anchor.get("type") in {"text", "image"}
                        or anchor.get("type") == "base"
                        and isinstance(anchor.get("target"), dict)
                        and (
                            anchor["target"].get("type") in {"group", "group_summary"}
                            or anchor["target"].get("type") == "timeline_range"
                            and isinstance(anchor["target"].get("lane"), dict)
                        )
                    )
                except (ValueError, UnicodeError, RecursionError):
                    is_anchor = False
                if not is_anchor and not group_capture:
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
            except (RequestValidationError, RecursionError):
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


@router.post("/shares/{share_id}/search")
def search(
    share_id: uuid.UUID,
    payload: DiscussionSearch,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    # POST keeps private search terms out of URL/access-log query strings.
    return discussion_search.search(db, token, share_id, payload)


@router.post("/commands")
def command(
    payload: CommandEnvelope, token: str = Depends(credential), db: Session = Depends(get_db)
):
    return service.apply_command(db, token, payload)


@router.post("/commands/withdraw")
def withdraw(
    payload: CommandEnvelope, token: str = Depends(credential), db: Session = Depends(get_db)
):
    return service.withdraw_command(db, token, payload)


@router.post("/commands/status")
def command_status(
    payload: CommandEnvelope,
    response: Response,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    # POST avoids logging the original private body in URLs. This route only
    # reads receipts; absence cannot create a tombstone or submit the command.
    response.headers["Cache-Control"] = "no-store"
    return service.command_status(db, token, payload)


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
    view_id: uuid.UUID | None = None,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.list_threads(
        db, token, share_id, entry_id, after=after, limit=limit, view_id=view_id
    )


@router.get("/shares/{share_id}/export-catalog")
def export_catalog(
    share_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    encoded = discussion_export.catalog(db, token, share_id)
    return Response(encoded, media_type="application/json", headers={"Cache-Control": "no-store"})


@router.get("/shares/{share_id}/entries/{entry_id}/export")
def export_snapshot(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    view_id: uuid.UUID | None = None,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    encoded = discussion_export.snapshot(db, token, share_id, entry_id, view_id=view_id)
    return Response(encoded, media_type="application/json", headers={"Cache-Control": "no-store"})


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


@router.get("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/anchor-history")
def anchor_history(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=20),
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.anchor_history(
        db, token, share_id, entry_id, thread_id, after=after, limit=limit
    )


@router.get("/shares/{share_id}/entries/{entry_id}/changes")
def changes(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=100),
    view_id: uuid.UUID | None = None,
    timeline_markers: bool = False,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.changes(
        db,
        token,
        share_id,
        entry_id,
        cursor=cursor,
        limit=limit,
        view_id=view_id,
        include_timeline_markers=timeline_markers,
    )


@router.post("/shares/{share_id}/entries/{entry_id}/moderation-catalog")
def moderation_catalog(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: ModerationRead,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return discussion_moderation.catalog(db, token, share_id, entry_id, request)


@router.post(
    "/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/messages/{message_id}/moderation-history"
)
def moderation_history(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    request: ModerationRead,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return discussion_moderation.history(
        db, token, share_id, entry_id, thread_id, message_id, request
    )


@router.post(
    "/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/messages/{message_id}"
    "/moderation-pictures/{picture_id}"
)
def moderation_picture_metadata(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    picture_id: uuid.UUID,
    request: ModerationReason,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return discussion_moderation.picture_metadata(
        db, token, share_id, entry_id, thread_id, message_id, picture_id, request
    )


@router.post(
    "/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/messages/{message_id}"
    "/moderation-pictures/{picture_id}/{variant}"
)
def moderation_picture_content(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    picture_id: uuid.UUID,
    variant: str,
    request: ModerationReason,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return discussion_moderation.picture_content(
        db, token, share_id, entry_id, thread_id, message_id, picture_id, variant, request
    )

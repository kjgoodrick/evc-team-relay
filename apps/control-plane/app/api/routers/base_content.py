import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.api.routers.discussions import credential
from app.db.session import get_db
from app.schemas.base_content import MAX_BASE64, PublishBaseContent
from app.services import base_content as service


class BoundedBaseContentRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method not in {"PUT", "POST"}:
                return await handler(request)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BASE64 + 8192:
                    raise HTTPException(413, {"code": "base_content_too_large"})
                body.extend(chunk)
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
                raise HTTPException(422, {"code": "invalid_base_content"}) from None

        return bounded


def require_preview():
    if not service.enabled():
        raise HTTPException(404, {"code": "base_content_disabled"})


router = APIRouter(
    prefix="/managed/shares/{share_id}/entries/{entry_id}/base-content",
    tags=["Bases content preview"],
    route_class=BoundedBaseContentRoute,
)


@router.get("", dependencies=[Depends(require_preview)])
def read(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.read(db, token, share_id, entry_id)


@router.put("", dependencies=[Depends(require_preview)])
def publish(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: PublishBaseContent,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: Annotated[
        uuid.UUID | None, Header(alias="X-Relay-Restore-Generation")
    ] = None,
):
    return service.publish(db, token, share_id, entry_id, payload, restore_generation)


@router.post("/withdraw")
def withdraw(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: PublishBaseContent,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: Annotated[
        uuid.UUID | None, Header(alias="X-Relay-Restore-Generation")
    ] = None,
):
    # Recovery remains available after new guarded saves are disabled.
    return service.withdraw(db, token, share_id, entry_id, payload, restore_generation)


@router.post("/status")
def command_status(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: PublishBaseContent,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    # Historical receipt inspection remains available with current read access.
    return service.command_status(db, token, share_id, entry_id, payload)

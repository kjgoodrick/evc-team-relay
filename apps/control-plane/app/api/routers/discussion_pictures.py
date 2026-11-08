import asyncio
import uuid
from typing import Literal

from anyio import CancelScope
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.routers.discussions import BoundedDiscussionRoute, credential, require_preview
from app.db.session import get_db
from app.schemas.discussion_pictures import PictureStage
from app.services import discussion_pictures as service

router = APIRouter(
    prefix="/collaboration/shares/{share_id}/entries/{entry_id}/pictures",
    tags=["discussion picture preview"],
    dependencies=[Depends(require_preview)],
    route_class=BoundedDiscussionRoute,
)


async def finish_worker(fn, *args):
    """Keep the dependency Session and admission slot alive until work exits.

    AnyIO shielding covers cancellation scopes; asyncio.shield additionally
    covers direct Task.cancel(), including client disconnect/server shutdown.
    Repeated cancellation must not release an executing decoder's admission.
    """
    job = asyncio.create_task(run_in_threadpool(fn, *args))
    cancelled = False
    with CancelScope(shield=True):
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
    if cancelled:
        if not job.cancelled():
            job.exception()
        raise asyncio.CancelledError
    return job.result()


@router.post("")
def stage(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: PictureStage,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    return service.stage(
        db, token, share_id, entry_id, payload, restore_generation=restore_generation
    )


@router.put("/{picture_id}/content")
async def upload(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    picture_id: uuid.UUID,
    request: Request,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    ticket = await finish_worker(
        service.prepare_upload, db, token, share_id, entry_id, picture_id, restore_generation
    )
    if not service.upload_slots.acquire(blocking=False):
        raise HTTPException(503, {"code": "picture_capacity"}, headers={"Retry-After": "2"})
    try:
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > ticket.size:
                        raise HTTPException(413, {"code": "picture_too_large"})
                    body.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, {"code": "picture_upload_timeout"}) from None
        data = bytes(body)
        del body
        return await finish_worker(service.upload, db, token, share_id, entry_id, ticket, data)
    finally:
        service.upload_slots.release()


@router.get("/{picture_id}")
def metadata(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    picture_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.metadata(db, token, share_id, entry_id, picture_id)


@router.get("/{picture_id}/{variant}")
def content(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    picture_id: uuid.UUID,
    variant: Literal["content", "thumbnail"],
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    data, mime = service.content(db, token, share_id, entry_id, picture_id, variant)
    return Response(
        data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "inline",
        },
    )

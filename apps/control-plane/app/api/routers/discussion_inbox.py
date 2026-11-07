import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.routers.discussions import BoundedDiscussionRoute, credential, require_preview
from app.db.session import get_db
from app.schemas.discussion_inbox import InboxUpdate, SubscriptionUpdate
from app.services import discussion_inbox as service

router = APIRouter(
    prefix="/collaboration",
    tags=["discussion preview"],
    dependencies=[Depends(require_preview)],
    route_class=BoundedDiscussionRoute,
)


@router.get("/shares/{share_id}/inbox")
def inbox(
    share_id: uuid.UUID,
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = False,
    include_muted: bool = False,
    include_dismissed: bool = False,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.list_items(
        db,
        token,
        share_id,
        cursor=cursor,
        limit=limit,
        unread_only=unread_only,
        include_muted=include_muted,
        include_dismissed=include_dismissed,
    )


@router.get("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/subscription")
def subscription(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.subscription(db, token, share_id, entry_id, thread_id)


@router.post("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/subscription")
def set_subscription(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    payload: SubscriptionUpdate,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.subscription(db, token, share_id, entry_id, thread_id, payload)


@router.post("/shares/{share_id}/entries/{entry_id}/threads/{thread_id}/inbox")
def set_inbox_state(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    thread_id: uuid.UUID,
    payload: InboxUpdate,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.update_item(db, token, share_id, entry_id, thread_id, payload)

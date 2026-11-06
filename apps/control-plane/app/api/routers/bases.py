import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.discussions import BoundedDiscussionRoute, credential
from app.db.session import get_db
from app.schemas.bases import BaseTargetCatalog, BaseTargetContext, InspectBase, ReconcileBase
from app.services import base_discussions, base_target_definitions
from app.services import bases_registry as service


def require_preview():
    if not service.enabled():
        raise HTTPException(404, {"code": "bases_registry_disabled"})


router = APIRouter(
    prefix="/collaboration/shares/{share_id}/entries/{entry_id}/bases",
    tags=["Bases registry preview"],
    dependencies=[Depends(require_preview)],
    route_class=BoundedDiscussionRoute,
)


@router.post("/target-context")
def target_context(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: BaseTargetContext,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return base_discussions.target_context(db, token, share_id, entry_id, payload)


@router.post("/inspect")
def inspect(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: InspectBase,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.synchronize(db, token, share_id, entry_id, payload)


@router.post("/target-definitions")
def target_definitions(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: BaseTargetCatalog,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return base_target_definitions.catalog(db, token, share_id, entry_id, payload)


@router.post("/reconcile")
def reconcile(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: ReconcileBase,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.synchronize(db, token, share_id, entry_id, payload)


@router.get("/views")
def views(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    state: Literal["unmatched", "archived"],
    after: uuid.UUID | None = None,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.list_views(db, token, share_id, entry_id, state, after)

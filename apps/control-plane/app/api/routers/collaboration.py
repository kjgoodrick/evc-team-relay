import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.db import models
from app.db.session import get_db
from app.schemas.collaboration import CommentRuleSet, CommentRuleUpdate, EntryCapabilities
from app.services import collaboration_policy, managed_service, share_service

router = APIRouter(prefix="/collaboration", tags=["collaboration"])


def require_policy_preview() -> None:
    if not get_settings().collaboration_policy_enabled:
        raise HTTPException(404, "Collaboration policy preview is disabled")


@router.get("/capabilities")
def capabilities(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
):
    share = share_service.get_share(db, share_id)
    managed_service.member(db, share, user)
    return {
        "protocol_version": 1,
        "features": {
            "comment_permissions": get_settings().collaboration_policy_enabled,
            "comments": False,
            "bases": False,
        },
    }


@router.get(
    "/shares/{share_id}/entries/{entry_id}/capabilities",
    response_model=EntryCapabilities,
    dependencies=[Depends(require_policy_preview)],
)
def entry_capabilities(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
):
    return collaboration_policy.entry_capabilities(
        db,
        share_service.get_share(db, share_id),
        user,
        str(entry_id),
    )


@router.get(
    "/shares/{share_id}/comment-rules",
    response_model=CommentRuleSet,
    dependencies=[Depends(require_policy_preview)],
)
def get_rules(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
):
    return collaboration_policy.get_rules(db, share_service.get_share(db, share_id), user)


@router.put(
    "/shares/{share_id}/comment-rules",
    response_model=CommentRuleSet,
    dependencies=[Depends(require_policy_preview)],
)
def put_rules(
    share_id: uuid.UUID,
    payload: CommentRuleUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(deps.get_current_user),
):
    return collaboration_policy.replace_rules(
        db,
        share_service.get_share(db, share_id),
        user,
        payload,
    )

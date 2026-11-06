import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models
from app.db.session import get_db
from app.schemas.collaboration import CommentRuleSet, CommentRuleUpdate, EntryCapabilities
from app.schemas.discussions import REACTION_KEYS
from app.services import (
    anchor_service,
    collaboration_auth,
    collaboration_policy,
    managed_service,
    share_service,
)

router = APIRouter(prefix="/collaboration", tags=["collaboration"])


def require_policy_preview() -> None:
    if not get_settings().collaboration_policy_enabled:
        raise HTTPException(404, "Collaboration policy preview is disabled")


@router.get("/capabilities")
def capabilities(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(collaboration_auth.get_actor),
):
    share = share_service.get_share(db, share_id)
    managed_service.member(db, share, user)
    return {
        "protocol_version": 1,
        "features": {
            "presence_locations_preview": get_settings().collaboration_presence_enabled,
            "comment_permissions": get_settings().collaboration_policy_enabled,
            "comments": False,
            "bases": False,
            "discussion_text_anchors": anchor_service.enabled(),
            "discussion_block_anchors": anchor_service.enabled(),
            "discussion_image_occurrences": anchor_service.enabled()
            and get_settings().collaboration_image_anchors_enabled,
            "discussion_commands": (
                get_settings().collaboration_policy_enabled
                and get_settings().collaboration_discussions_enabled
            ),
        },
        "discussion_targets": (
            ["note", "text"]
            + (["image"] if get_settings().collaboration_image_anchors_enabled else [])
        )
        if anchor_service.enabled()
        else ["note"],
        "discussion_body_version": 1,
        "discussion_session_required": True,
        "reaction_keys": REACTION_KEYS,
        "discussion_limits": {
            "message_bytes": 32768,
            "command_bytes": 256 * 1024 if anchor_service.enabled() else 65536,
            "note_command_bytes": 65536,
            "anchor_dependency_bytes": 65536,
            "page_items": 100,
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
    user: models.User = Depends(collaboration_auth.get_actor),
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
    user: models.User = Depends(collaboration_auth.get_actor),
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
    user: models.User = Depends(collaboration_auth.get_actor),
):
    return collaboration_policy.replace_rules(
        db,
        share_service.get_share(db, share_id),
        user,
        payload,
    )

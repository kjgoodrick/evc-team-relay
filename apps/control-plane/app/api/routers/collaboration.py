import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models
from app.db.session import get_db
from app.schemas.collaboration import (
    CommentRuleSet,
    CommentRuleUpdate,
    EntryCapabilities,
    PermissionPolicy,
    PermissionPolicyUpdate,
)
from app.schemas.discussions import REACTION_KEYS
from app.services import (
    anchor_service,
    base_discussions,
    base_file_group_values,
    base_presence_formulas,
    bases_registry,
    collaboration_auth,
    collaboration_policy,
    discussion_export,
    discussion_inbox,
    discussion_moderation,
    discussion_pictures,
    discussion_reattachment,
    discussion_search,
    managed_service,
    permission_editor,
    share_service,
    timeline_markers,
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
            "presence_surface_locations_preview": get_settings().collaboration_presence_enabled,
            "presence_surface_roster_preview": get_settings().collaboration_presence_enabled,
            "presence_image_context_preview": get_settings().collaboration_presence_enabled,
            "presence_base_context_preview": get_settings().collaboration_presence_enabled
            and bases_registry.enabled(),
            "presence_base_states_preview": get_settings().collaboration_presence_enabled
            and bases_registry.enabled(),
            "presence_base_targets_v2_preview": get_settings().collaboration_presence_enabled
            and bases_registry.enabled(),
            "presence_base_targets_v3_preview": base_presence_formulas.enabled(),
            "presence_image_states_preview": get_settings().collaboration_presence_enabled,
            "presence_embedded_images_preview": get_settings().collaboration_presence_enabled,
            "presence_multi_watch_preview": get_settings().collaboration_presence_enabled,
            "comment_permissions": get_settings().collaboration_policy_enabled,
            "permission_editor_v1": get_settings().collaboration_policy_enabled,
            "comments": False,
            "bases": False,
            "bases_registry": bases_registry.enabled(),
            "discussion_bases": base_discussions.enabled(),
            "discussion_base_definition_catalog": base_discussions.enabled(),
            "discussion_base_groups": base_discussions.groups_enabled(),
            "discussion_base_formula_groups": base_discussions.formula_groups_enabled(),
            "discussion_timeline_ranges": base_discussions.timeline_ranges_enabled(),
            "discussion_timeline_markers": timeline_markers.enabled(),
            "bases_sync": get_settings().managed_bases_sync_enabled,
            "discussion_text_anchors": anchor_service.enabled(),
            "discussion_block_anchors": anchor_service.enabled(),
            "discussion_image_occurrences": anchor_service.enabled()
            and get_settings().collaboration_image_anchors_enabled,
            "discussion_image_files": (
                get_settings().collaboration_policy_enabled
                and get_settings().collaboration_discussions_enabled
                and get_settings().collaboration_image_files_enabled
            ),
            "discussion_pictures": discussion_pictures.enabled(),
            "discussion_inbox": discussion_inbox.enabled(),
            "discussion_search": discussion_search.enabled(),
            "discussion_export": discussion_export.enabled(),
            "discussion_moderation_recovery": discussion_moderation.enabled(),
            "discussion_moderation_catalog": discussion_moderation.enabled(),
            "discussion_export_catalog": discussion_export.enabled(),
            "discussion_reattach": discussion_reattachment.enabled(),
            "discussion_recovery": (
                get_settings().collaboration_policy_enabled
                and get_settings().collaboration_discussions_enabled
            ),
            "discussion_command_status": (
                get_settings().collaboration_policy_enabled
                and get_settings().collaboration_discussions_enabled
            ),
            "discussion_commands": (
                get_settings().collaboration_policy_enabled
                and get_settings().collaboration_discussions_enabled
            ),
        },
        "discussion_targets": ["note"]
        + (["text"] if anchor_service.enabled() else [])
        + (
            ["image"]
            if anchor_service.enabled() and get_settings().collaboration_image_anchors_enabled
            else []
        )
        + (
            ["image_file"]
            if get_settings().collaboration_policy_enabled
            and get_settings().collaboration_discussions_enabled
            and get_settings().collaboration_image_files_enabled
            else []
        )
        + (["base"] if base_discussions.enabled() else []),
        "discussion_base_targets": ["view", "item", "cell", "property"]
        + (["formula", "summary"] if base_discussions.definitions_enabled() else [])
        + (["formula_cell"] if base_discussions.formula_cells_enabled() else [])
        + (["group"] if base_discussions.groups_enabled() else [])
        + (["group_summary"] if base_discussions.group_summaries_enabled() else [])
        + (["timeline_range"] if base_discussions.timeline_ranges_enabled() else [])
        + (["timeline_marker"] if timeline_markers.enabled() else [])
        if base_discussions.enabled()
        else [],
        "discussion_base_file_group_properties": list(base_file_group_values.PROPERTIES)
        if base_discussions.groups_enabled()
        else [],
        "discussion_base_record_types": list(base_discussions.RECORD_TYPES)
        if base_discussions.enabled()
        else [],
        "discussion_body_version": 1,
        "discussion_session_required": True,
        "reaction_keys": REACTION_KEYS,
        "discussion_limits": {
            "message_bytes": 32768,
            "command_bytes": 256 * 1024 if anchor_service.enabled() else 65536,
            "note_command_bytes": 65536,
            "anchor_dependency_bytes": 65536,
            "page_items": 100,
            "picture_bytes": 10 * 1024 * 1024,
            "picture_pixels": 20_000_000,
            "pictures_per_message": 10,
        },
        "discussion_picture_formats": ["image/png", "image/jpeg", "image/webp", "image/gif"],
        "discussion_picture_animation": False,
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


@router.get(
    "/shares/{share_id}/permissions",
    response_model=PermissionPolicy,
    dependencies=[Depends(require_policy_preview)],
)
def get_permission_policy(
    share_id: uuid.UUID,
    actor_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: models.User = Depends(collaboration_auth.get_actor),
):
    return permission_editor.get_policy(db, share_service.get_share(db, share_id), user, actor_id)


@router.put(
    "/shares/{share_id}/permissions",
    response_model=PermissionPolicy,
    dependencies=[Depends(require_policy_preview)],
)
def put_permission_policy(
    share_id: uuid.UUID,
    payload: PermissionPolicyUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(collaboration_auth.get_actor),
):
    return permission_editor.replace_policy(
        db, share_service.get_share(db, share_id), user, payload
    )

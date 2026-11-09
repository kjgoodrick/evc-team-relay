import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.discussions import BoundedDiscussionRoute, credential
from app.db.session import get_db
from app.schemas.bases import (
    BaseRecordContexts,
    BaseTargetCatalog,
    BaseTargetContext,
    BaseTimelineAxisContext,
    CaptureBaseGroup,
    InspectBase,
    ReconcileBase,
)
from app.schemas.timeline_markers import TimelineMarkerCatalog, TimelineMarkerCommand
from app.services import (
    base_discussions,
    base_groups,
    base_target_definitions,
    inline_base_source,
    timeline_axes,
    timeline_markers,
)
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


@router.post("/record-contexts")
def record_contexts(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: BaseRecordContexts,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return base_discussions.record_contexts(db, token, share_id, entry_id, payload)


@router.post("/group-targets")
def capture_group(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: CaptureBaseGroup,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    return base_groups.capture(
        db, token, share_id, entry_id, payload, restore_generation=restore_generation
    )


@router.post("/timeline-axis")
def timeline_axis(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: BaseTimelineAxisContext,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return timeline_axes.context(db, token, share_id, entry_id, payload)


@router.post("/inspect")
def inspect(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: InspectBase,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.synchronize(db, token, share_id, entry_id, payload)


@router.post("/timeline-markers/catalog")
def timeline_marker_catalog(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: TimelineMarkerCatalog,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return timeline_markers.list_markers(db, token, share_id, entry_id, payload)


@router.post("/timeline-markers/commands")
def timeline_marker_command(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: TimelineMarkerCommand,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    return timeline_markers.apply_command(
        db, token, share_id, entry_id, payload, restore_generation=restore_generation
    )


@router.post("/timeline-markers/commands/status")
def timeline_marker_command_status(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: TimelineMarkerCommand,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return timeline_markers.command_status(db, token, share_id, entry_id, payload)


@router.get("/timeline-markers/{marker_id}")
def timeline_marker(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    marker_id: uuid.UUID,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return timeline_markers.read_marker(db, token, share_id, entry_id, marker_id)


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
    restore_generation: uuid.UUID | None = Header(None, alias="X-Relay-Restore-Generation"),
):
    return service.synchronize(db, token, share_id, entry_id, payload, restore_generation)


@router.post("/reconcile/status")
def repair_status(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: ReconcileBase,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.repair_status(db, token, share_id, entry_id, payload)


@router.get("/views")
def views(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    state: Literal["unmatched", "archived"],
    after: uuid.UUID | None = None,
    definition_id: uuid.UUID | None = None,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return service.list_views(db, token, share_id, entry_id, state, after, definition_id)


@router.post("/inline-source")
def inline_source(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: inline_base_source.InspectInlineSource,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return inline_base_source.inspect(db, token, share_id, entry_id, payload)


@router.post("/inline-inspect")
def inline_inspect(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: inline_base_source.InspectInlineSource,
    token: str = Depends(credential),
    db: Session = Depends(get_db),
):
    return inline_base_source.inspect(db, token, share_id, entry_id, payload, persist=True)

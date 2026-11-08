"""Bounded, process-local location subscriptions. No cursor history or activity feed.

All broker mutation runs on the collaboration socket's event loop. Database
checks use fresh sessions in worker threads. Deploy this opt-in channel to one
designated process until an interprocess ephemeral bus is implemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import HTTPException
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import and_, select

from app.api import deps
from app.core import security
from app.db import models
from app.db.session import get_sessionmaker
from app.schemas.timeline_ranges import TimelineRangeValue
from app.services import bases_registry, managed_service, share_service
from app.services.presence_protocol import PresenceIdentity, identity_for, relative_position

MAX_CONNECTIONS = 128
MAX_OBSERVATIONS = 8
MAX_OUTPUT_BURST = 2 * 1024 * 1024
OUTPUT_BYTES_PER_SECOND = 1024 * 1024
MAX_TICKETS = 512
TICKET_SECONDS = 30
IDLE_SECONDS = 30
CHECK_SECONDS = 5
MAX_MESSAGE_BYTES = 4096
MAX_ROSTER_BYTES = 192 * 1024
MAX_BASE_TARGET_BYTES = 512 * 1024
# Ingress already coalesces to eight ordinary movements/second per client.
# Batch concurrent peers for one frame without adding a second 100 ms delay.
IMAGE_STATE_INTERVAL = 0.016
PeerKey = tuple[str, str, str]


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["note"]
    share_id: uuid.UUID
    entry_id: uuid.UUID
    surface: Literal["editor", "reading"]


class ImageLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["image"]
    share_id: uuid.UUID
    entry_id: uuid.UUID
    generation: uuid.UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImagePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    x: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


class ImageViewport(ImagePoint):
    width: float = Field(strict=True, gt=0, le=1, allow_inf_nan=False)
    height: float = Field(strict=True, gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def within_image(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Viewport outside image")
        return self


class ImageState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pointer: ImagePoint | None
    viewport: ImageViewport | None


class ManagedEmbedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["managed"]
    entry_id: uuid.UUID
    generation: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalEmbedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["external"]
    url_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version_evidence: Literal["unknown"]


class ImageBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    width: int = Field(strict=True, ge=1, le=1_000_000)
    height: int = Field(strict=True, ge=1, le=1_000_000)
    orientation: Literal["display"]


class EmbeddedImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=1)
    occurrence: dict
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: Annotated[ManagedEmbedAsset | ExternalEmbedAsset, Field(discriminator="kind")]
    basis: ImageBasis
    state: ImageState

    @field_validator("occurrence")
    @classmethod
    def valid_occurrence(cls, value):
        if set(value) != {"anchor", "head"} or not all(
            relative_position(value[key]) for key in value
        ):
            raise ValueError("Invalid image occurrence")
        return value

    def semantic_key(self) -> str:
        # Geometry updates do not invalidate a pending permission check; moving
        # to a different occurrence/asset does. JSON keys are client orderable.
        return json.dumps(
            self.model_dump(mode="json", exclude={"state"}), sort_keys=True, separators=(",", ":")
        )


class BaseLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["base"]
    share_id: uuid.UUID
    entry_id: uuid.UUID
    generation: uuid.UUID
    definition_id: uuid.UUID
    view_id: uuid.UUID
    registry_revision: int = Field(strict=True, ge=1, le=9007199254740991)


class BasePresenceProperty(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["note", "file", "formula"]
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Invalid property name")
        if len(value.encode("utf-8")) > 256:
            raise ValueError("Property name too large")
        return value


class BasePresenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["item"]
    record_id: uuid.UUID
    record_generation: uuid.UUID


class BasePresenceCell(BasePresenceItem):
    type: Literal["cell"]
    property: BasePresenceProperty


class BasePresenceHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["property"]
    property: BasePresenceProperty


class BasePresenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_id: uuid.UUID
    record_generation: uuid.UUID


class BasePresenceGroup(BasePresenceItem):
    type: Literal["group"]
    property: BasePresenceProperty
    value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    references: tuple[BasePresenceReference, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def supported_group(self):
        from app.services.base_file_group_values import PROPERTIES

        if self.property.source == "file" and self.property.name.lower() not in PROPERTIES:
            raise ValueError("Unsupported presence group")
        ids = [self.record_id, *(ref.record_id for ref in self.references)]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate group reference")
        return self


class BasePresenceAxis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=1)
    renderer: Literal["sabidurian-timeline", "sabidurian-sequence"]
    start_property: BasePresenceProperty
    end_property: BasePresenceProperty | None
    time_zone: str | None = Field(max_length=128)


class BasePresenceRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["timeline_range"]
    axis: BasePresenceAxis
    range: TimelineRangeValue
    lane: BasePresenceGroup | None

    @model_validator(mode="after")
    def coordinate_system(self):
        from app.schemas.timeline_ranges import zone

        sequence = self.axis.renderer == "sabidurian-sequence"
        if (
            sequence != (self.range["kind"] == "ordinal")
            or sequence
            and self.axis.time_zone is not None
        ):
            raise ValueError("Timeline coordinate system mismatch")
        if self.axis.time_zone is not None:
            zone(self.axis.time_zone)
            if "time_zone" in self.range and self.range["time_zone"] != self.axis.time_zone:
                raise ValueError("Timeline timezone mismatch")
        return self


class BasePosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target: Annotated[
        BasePresenceItem
        | BasePresenceCell
        | BasePresenceHeader
        | BasePresenceGroup
        | BasePresenceRange,
        Field(discriminator="type"),
    ]
    offset: ImagePoint


class BaseState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=3)
    pointer: BasePosition | None
    viewport: BasePosition | None

    @model_validator(mode="after")
    def negotiated_targets(self):
        if self.version < 3 and any(
            position and formula_group_target(position.target)
            for position in (self.pointer, self.viewport)
        ):
            raise ValueError("Formula groups require version three")
        if self.version == 1 and any(
            position and isinstance(position.target, (BasePresenceGroup, BasePresenceRange))
            for position in (self.pointer, self.viewport)
        ):
            raise ValueError("Extended targets require version two")
        if len(self.model_dump_json().encode("utf-8")) > 3072:
            raise ValueError("Bases position exceeds state budget")
        return self

    def for_version(self, version: int) -> dict:
        result = self.model_dump(mode="json")
        result["version"] = min(self.version, version)
        if version < 3:
            for field in ("pointer", "viewport"):
                position = getattr(self, field)
                if position and formula_group_target(position.target):
                    result[field] = None
        if version == 1:
            result["version"] = 1
            for field in ("pointer", "viewport"):
                if result[field] and result[field]["target"]["type"] in {"group", "timeline_range"}:
                    result[field] = None
        return result

    def semantic_key(self) -> str:
        # Pointer movement within one cell is replaceable geometry. A different
        # cell/record/header must invalidate pending authorization and sends.
        return json.dumps(
            {
                key: value.target.model_dump(mode="json") if value else None
                for key, value in (("pointer", self.pointer), ("viewport", self.viewport))
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def formula_group_target(target):
    group = target.lane if isinstance(target, BasePresenceRange) else target
    return isinstance(group, BasePresenceGroup) and group.property.source == "formula"


class BaseRecordContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    share_id: uuid.UUID
    entry_id: uuid.UUID
    generation: uuid.UUID
    type: Literal["markdown", "image", "file"]


class BasePresenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: uuid.UUID
    slot: int = Field(strict=True, ge=0, le=63)
    name: str = Field(min_length=1, max_length=256)
    type: str = Field(min_length=1, max_length=256)

    @field_validator("name", "type")
    @classmethod
    def safe_label(cls, value):
        if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Invalid saved-view label")
        return value


class BasePresenceFormulaGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    view_id: uuid.UUID
    property: BasePresenceProperty
    inputs: list[BasePresenceProperty] = Field(max_length=32)


class BasePresenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    share_id: uuid.UUID
    entry_id: uuid.UUID
    generation: uuid.UUID
    definition_id: uuid.UUID
    registry_revision: int = Field(strict=True, ge=1, le=9007199254740991)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    views: list[BasePresenceView] = Field(max_length=64)
    formula_groups: list[BasePresenceFormulaGroup] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def unique_views(self):
        for attribute in ("id", "slot", "name"):
            if len({getattr(view, attribute) for view in self.views}) != len(self.views):
                raise ValueError("Ambiguous saved views")
        if [view.slot for view in self.views] != sorted(view.slot for view in self.views):
            raise ValueError("Unordered saved views")
        return self


SurfaceLocation = Location | ImageLocation | BaseLocation
_surface_location = TypeAdapter(Annotated[SurfaceLocation, Field(discriminator="kind")])


def parse_location(value, protocol_version: int) -> SurfaceLocation:
    # Version one remains Markdown-only, including strict unknown-field checks.
    if protocol_version == 1:
        return Location.model_validate(value)
    if protocol_version == 2:
        return _surface_location.validate_python(value)
    raise ValueError("Unsupported presence protocol")


def supports_location(protocol_version: int, location: SurfaceLocation) -> bool:
    return protocol_version == 2 or isinstance(location, Location)


def same_context(left: SurfaceLocation, right: SurfaceLocation) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Location):
        # Source, Live Preview and Reading share the same authorized note room.
        return left.share_id == right.share_id and left.entry_id == right.entry_id
    # Other rooms are pinned to the same image bytes or saved-view identity.
    # A different Bases view is not consent to discover this view's participants.
    return left == right


def room_location(location: SurfaceLocation) -> SurfaceLocation:
    return (
        location.model_copy(update={"surface": "editor"})
        if isinstance(location, Location)
        else location
    )


def authorize_location(db, user, location: SurfaceLocation) -> None:
    share = share_service.get_share(db, location.share_id)
    entry = managed_service.authorize_entry(db, share, user, str(location.entry_id))
    valid = False
    if isinstance(location, Location):
        valid = entry.record.get("type") == "markdown"
    elif isinstance(location, ImageLocation):
        valid = (
            entry.record.get("type") == "image"
            and entry.collaboration_generation == location.generation
            and entry.record.get("hash") == location.content_sha256
        )
    elif isinstance(location, BaseLocation) and bases_registry.enabled():
        # Select only identity metadata. Frequent presence revalidation must not
        # materialize the saved configuration or view descriptors.
        definition, view = models.BaseDefinition, models.BaseView
        valid = bool(
            entry.record.get("type") == "file"
            and entry.path.lower().endswith(".base")
            and entry.collaboration_generation == location.generation
            and db.scalar(
                select(view.id)
                .join(definition, view.definition_id == definition.id)
                .where(
                    definition.id == location.definition_id,
                    definition.entry_id == entry.id,
                    definition.generation == location.generation,
                    definition.revision == location.registry_revision,
                    definition.content_sha256 == entry.record.get("hash"),
                    view.id == location.view_id,
                    view.state == "active",
                )
            )
        )
    if not valid:
        raise HTTPException(403, "Location unavailable")


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: uuid.UUID
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    instance_id: uuid.UUID

    def key(self) -> PeerKey:
        return str(self.user_id), self.session_id, str(self.instance_id)


@dataclass(frozen=True)
class Actor:
    identity: PresenceIdentity
    expires_at: int

    def key(self, instance_id: str) -> PeerKey:
        assert self.identity.session_id
        return self.identity.user_id, self.identity.session_id, instance_id


def authorize_embed(db, user, location: Location, embed: EmbeddedImage) -> None:
    if isinstance(embed.asset, ManagedEmbedAsset):
        authorize_location(
            db,
            user,
            ImageLocation(
                kind="image",
                share_id=location.share_id,
                entry_id=embed.asset.entry_id,
                generation=embed.asset.generation,
                content_sha256=embed.asset.sha256,
            ),
        )


def authorize_base_state(db, user, location: BaseLocation, state: BaseState) -> None:
    from app.services import base_presence_formulas, base_target_definitions, timeline_axes

    share = share_service.get_share(db, location.share_id)
    records = {}
    descriptor = None
    axis_candidate = None
    configuration = None

    def config():
        nonlocal configuration
        if configuration is None:
            configuration = (
                db.scalar(
                    select(models.BaseDefinition.configuration).where(
                        models.BaseDefinition.id == location.definition_id
                    )
                )
                or {}
            )
        return configuration

    def record(record_id, generation, *, markdown=False):
        if record_id not in records:
            records[record_id] = managed_service.authorize_entry(db, share, user, str(record_id))
        entry = records[record_id]
        if (
            entry.record.get("type") not in ("markdown", "image", "file")
            or entry.collaboration_generation != generation
            or markdown
            and entry.record.get("type") != "markdown"
        ):
            raise HTTPException(403, "Position unavailable")

    def view():
        nonlocal descriptor
        if descriptor is None:
            descriptor = db.scalar(
                select(models.BaseView.descriptor).where(
                    models.BaseView.id == location.view_id,
                    models.BaseView.definition_id == location.definition_id,
                    models.BaseView.state == "active",
                )
            )
        if not isinstance(descriptor, dict):
            raise HTTPException(403, "Position unavailable")
        return descriptor

    def group(target):
        grouping = view().get("groupBy")
        if (
            not isinstance(grouping, dict)
            or set(grouping) - {"property", "direction"}
            or base_target_definitions.property_locator(grouping.get("property"))
            != target.property.model_dump(mode="json")
        ):
            raise HTTPException(403, "Position unavailable")
        if target.property.source == "formula" and (
            not base_presence_formulas.enabled()
            or base_presence_formulas.plan(config(), view()) is None
        ):
            raise HTTPException(403, "Position unavailable")
        # A fingerprint is only a local matching hint, never an authority proof.
        # Both parties must read the selected member and every linked input.
        record(
            target.record_id,
            target.record_generation,
            markdown=target.property.source in {"note", "formula"},
        )
        for reference in target.references:
            record(reference.record_id, reference.record_generation)

    def axis():
        nonlocal axis_candidate
        if axis_candidate is None:
            saved = view()
            properties = [
                base_target_definitions.property_locator(saved.get(key))
                for key in (
                    ("orderProp", "orderEndProp")
                    if saved.get("type") == "sabidurian-sequence"
                    else ("startDateProp", "endDateProp")
                )
            ]
            axis_candidate = timeline_axes.candidate(
                config()
                if any(prop and prop["source"] == "formula" for prop in properties)
                else {},
                saved,
            )
        return axis_candidate

    for position in (state.pointer, state.viewport):
        if not position:
            continue
        target = position.target
        if isinstance(target, BasePresenceGroup):
            group(target)
        elif isinstance(target, BasePresenceRange):
            # Only axis metadata is inspected. Do not evaluate formulas, read
            # note contents, create discussion IDs or persist pointer history.
            candidate = axis()
            if not candidate or candidate[0]["axis"] != target.axis.model_dump(mode="json"):
                raise HTTPException(403, "Position unavailable")
            if target.lane is not None:
                group(target.lane)
        elif isinstance(target, BasePresenceItem):
            record(
                target.record_id,
                target.record_generation,
                markdown=isinstance(target, BasePresenceCell) and target.property.source == "note",
            )


def authenticate(
    token: str,
    location: SurfaceLocation | None = None,
    embed: EmbeddedImage | None = None,
    base_state: BaseState | None = None,
) -> Actor:
    with get_sessionmaker()() as db:
        user = deps._get_user_from_token(db, token)
        try:
            claims = security.decode_access_token(token)
        except InvalidTokenError:
            raise HTTPException(401, "Invalid or expired token") from None
        if not claims.get("session_id") or type(claims.get("exp")) is not int:
            raise HTTPException(401, "Session-backed credentials required")
        if location:
            authorize_location(db, user, location)
        if embed:
            if not isinstance(location, Location):
                raise HTTPException(403, "Location unavailable")
            authorize_embed(db, user, location, embed)
        if base_state:
            if not isinstance(location, BaseLocation):
                raise HTTPException(403, "Location unavailable")
            authorize_base_state(db, user, location, base_state)
        return Actor(identity_for(db, user, claims), int(claims["exp"]))


def authorize_delivery(viewer_token: str, publisher_token: str, location: SurfaceLocation) -> None:
    # Both users must still have live sessions and read access to this exact
    # destination. A revoked publisher cannot keep an old location visible.
    authenticate(viewer_token, location)
    authenticate(publisher_token, location)


def image_location(token: str, share_id: uuid.UUID, entry_id: uuid.UUID) -> ImageLocation:
    # Presence identity discovery does not depend on discussion capabilities or
    # comment permission. Return no filename, asset URL or content bytes.
    authenticate(token)
    with get_sessionmaker()() as db:
        user = deps._get_user_from_token(db, token)
        share = share_service.get_share(db, share_id)
        entry = managed_service.authorize_entry(db, share, user, str(entry_id))
        if entry.record.get("type") != "image":
            raise HTTPException(403, "Location unavailable")
        try:
            return ImageLocation(
                kind="image",
                share_id=share_id,
                entry_id=entry_id,
                generation=entry.collaboration_generation,
                content_sha256=entry.record.get("hash"),
            )
        except ValueError:
            raise HTTPException(403, "Location unavailable") from None


def base_context(
    token: str, share_id: uuid.UUID, entry_id: uuid.UUID, base_target_version=1
) -> BasePresenceContext:
    """Read existing active identities only: no inspection, repair or object I/O.

    Presence must not initialize a registry or change the saved configuration.
    Clients prove their native source against this hash before choosing a slot.
    Unmatched/archived descriptors, formula text, filters and record data stay out.
    Version-three callers may receive bounded static formula-input names only.
    """
    from app.services import base_presence_formulas

    authenticate(token)
    with get_sessionmaker()() as db:
        user = deps._get_user_from_token(db, token)
        share = share_service.get_share(db, share_id)
        entry = managed_service.authorize_entry(db, share, user, str(entry_id))
        if entry.record.get("type") != "file" or not entry.path.lower().endswith(".base"):
            raise HTTPException(403, "Location unavailable")
        definition, view = models.BaseDefinition, models.BaseView
        # One SQL statement pins descriptors and revision to the same database
        # snapshot even when reconciliation commits concurrently.
        rows = db.execute(
            select(
                definition.id,
                definition.generation,
                definition.revision,
                definition.content_sha256,
                view.id.label("view_id"),
                view.slot,
                view.descriptor["name"].label("name"),
                view.descriptor["type"].label("type"),
                view.descriptor,
            )
            .outerjoin(view, and_(view.definition_id == definition.id, view.state == "active"))
            .where(
                definition.entry_id == entry.id,
                definition.generation == entry.collaboration_generation,
                definition.content_sha256 == entry.record.get("hash"),
            )
            .order_by(view.slot, view.id)
            .limit(65)
        ).all()
        if not rows:
            raise HTTPException(403, "Location unavailable")
        current = rows[0]
        formula_groups = None
        if base_target_version >= 3 and base_presence_formulas.enabled():
            # Fetch the configuration once, only for opted-in callers. Exact
            # revision/hash predicates prevent mixing a concurrent reconcile.
            configuration = db.scalar(
                select(definition.configuration).where(
                    definition.id == current.id,
                    definition.revision == current.revision,
                    definition.content_sha256 == current.content_sha256,
                )
            )
            formula_groups, remaining = [], 16_382
            for row in rows:
                candidate = base_presence_formulas.plan(configuration, row.descriptor)
                if row.view_id is None or candidate is None:
                    continue
                candidate = {"view_id": str(row.view_id), **candidate}
                size = len(json.dumps(candidate, ensure_ascii=True)) + 2
                if size <= remaining:
                    formula_groups.append(candidate)
                    remaining -= size
        try:
            return BasePresenceContext(
                share_id=share_id,
                entry_id=entry_id,
                generation=current.generation,
                definition_id=current.id,
                registry_revision=current.revision,
                content_sha256=current.content_sha256,
                views=[
                    BasePresenceView(id=row.view_id, slot=row.slot, name=row.name, type=row.type)
                    for row in rows
                    if row.view_id is not None
                ],
                formula_groups=formula_groups,
            )
        except (ValueError, AttributeError):
            raise HTTPException(403, "Location unavailable") from None


def base_record_context(token: str, share_id: uuid.UUID, entry_id: uuid.UUID) -> BaseRecordContext:
    # Presence never initializes missing lineage or requires comment permission.
    authenticate(token)
    with get_sessionmaker()() as db:
        user = deps._get_user_from_token(db, token)
        share = share_service.get_share(db, share_id)
        entry = managed_service.authorize_entry(db, share, user, str(entry_id))
        try:
            return BaseRecordContext(
                share_id=share_id,
                entry_id=entry_id,
                generation=entry.collaboration_generation,
                type=entry.record.get("type"),
            )
        except ValueError:
            raise HTTPException(403, "Position unavailable") from None


def base_state_snapshot(
    viewer_token: str, location: BaseLocation, publishers: list[tuple[PeerKey, str, BaseState]]
) -> list[dict]:
    # Reading a Base does not grant access to every record in its results.
    # Denied precise state is omitted without removing the saved-view roster.
    authenticate(viewer_token, location)
    result = []
    with get_sessionmaker()() as db:
        viewer = deps._get_user_from_token(db, viewer_token)
        for key, token, state in publishers:
            try:
                actor = authenticate(token, location, base_state=state)
                authorize_base_state(db, viewer, location, state)
            except HTTPException:
                continue
            if actor.key(key[2]) == key:
                result.append(
                    {
                        "target": {"user_id": key[0], "session_id": key[1], "instance_id": key[2]},
                        "state": state.model_dump(mode="json"),
                    }
                )
    return result


def roster_snapshot(
    viewer_token: str, location: SurfaceLocation, publishers: list[tuple[PeerKey, str]]
) -> list[dict]:
    """One viewer check, then fresh identity/access for each bounded participant.

    This runs off the event loop. The caller fences the captured room and tokens
    before sending. No client-supplied name, profile, path or activity is relayed.
    """
    authenticate(viewer_token, location)
    result = []
    for key, token in publishers:
        try:
            actor = authenticate(token, location)
        except HTTPException:
            continue
        if actor.key(key[2]) == key:
            result.append(
                {
                    "target": {"user_id": key[0], "session_id": key[1], "instance_id": key[2]},
                    "name": actor.identity.name,
                }
            )
    return result


def embed_snapshot(
    viewer_token: str, location: Location, publishers: list[tuple[PeerKey, str, EmbeddedImage]]
) -> list[dict]:
    # A note reader is not necessarily allowed to read its embedded image.
    # Omit the entire embed (including hashes and IDs) for either denied party.
    authenticate(viewer_token, location)
    result = []
    with get_sessionmaker()() as db:
        viewer = deps._get_user_from_token(db, viewer_token)
        for key, token, embed in publishers:
            try:
                actor = authenticate(token, location, embed)
                authorize_embed(db, viewer, location, embed)
            except HTTPException:
                continue
            if actor.key(key[2]) == key:
                result.append(
                    {
                        "target": {"user_id": key[0], "session_id": key[1], "instance_id": key[2]},
                        "embed": embed.model_dump(mode="json"),
                    }
                )
    return result


@dataclass
class Ticket:
    token: str
    user_id: str
    expires: float


@dataclass(frozen=True)
class Follow:
    subscription_id: str
    target: PeerKey


@dataclass
class WatchDelivery:
    roster_pending: bool = False
    states_pending: bool = False
    last_roster: bytes | None = None
    last_image_states: bytes | None = None


@dataclass(frozen=True)
class Watch:
    subscription_id: str
    location: SurfaceLocation
    image_states: bool = False
    embedded_images: bool = False
    base_states: bool = False
    base_target_version: int = 1
    delivery: WatchDelivery = field(default_factory=WatchDelivery, compare=False)


@dataclass(eq=False)
class Connection:
    token: str
    actor: Actor
    instance_id: str
    protocol_version: int = 1
    location: SurfaceLocation | None = None
    image_state: ImageState | None = None
    image_embed: EmbeddedImage | None = None
    embed_revision: int = 0
    base_state: BaseState | None = None
    base_revision: int = 0
    follow: Follow | None = None
    watch: Watch | None = None
    observations: dict[str, Watch] = field(default_factory=dict)
    revision: int = 0
    closed: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    roster_dirty: asyncio.Event = field(default_factory=asyncio.Event)
    image_dirty: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    controls: list[dict] = field(default_factory=list)
    last_location: dict | None = None
    output_allowance: float = MAX_OUTPUT_BURST
    output_time: float = field(default_factory=time.monotonic)
    allowance: float = 20
    allowance_time: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> PeerKey:
        return self.actor.key(self.instance_id)

    def watches(self) -> tuple[Watch, ...]:
        return ((self.watch,) if self.watch else ()) + tuple(self.observations.values())

    def has_watch(self, watch: Watch) -> bool:
        return self.watch is watch or self.observations.get(watch.subscription_id) is watch

    def permit_output(self, size: int) -> bool:
        now = time.monotonic()
        self.output_allowance = min(
            MAX_OUTPUT_BURST,
            self.output_allowance + (now - self.output_time) * OUTPUT_BYTES_PER_SECOND,
        )
        self.output_time = now
        if size > self.output_allowance:
            return False
        self.output_allowance -= size
        return True

    def permit_message(self) -> bool:
        now = time.monotonic()
        self.allowance = min(20, self.allowance + (now - self.allowance_time) * 10)
        self.allowance_time = now
        if self.allowance < 1:
            return False
        self.allowance -= 1
        return True

    def control(self, message: dict) -> None:
        if len(self.controls) >= 16:
            raise OverflowError("Slow collaboration consumer")
        self.controls.append(message)
        self.dirty.set()


class PresenceBroker:
    def __init__(self):
        self.tickets: dict[str, Ticket] = {}
        self.connections: dict[PeerKey, Connection] = {}
        self.followers: dict[PeerKey, set[Connection]] = {}
        self.rooms: dict[SurfaceLocation, set[Connection]] = {}
        self.watchers: dict[SurfaceLocation, set[Connection]] = {}
        self.handshakes = 0

    def ticket(self, token: str, actor: Actor) -> str:
        now = time.monotonic()
        self.tickets = {key: item for key, item in self.tickets.items() if item.expires > now}
        if (
            len(self.tickets) >= MAX_TICKETS
            or sum(item.user_id == actor.identity.user_id for item in self.tickets.values()) >= 8
        ):
            raise HTTPException(429, "Too many pending collaboration tickets")
        value = secrets.token_urlsafe(32)
        self.tickets[hashlib.sha256(value.encode()).hexdigest()] = Ticket(
            token, actor.identity.user_id, now + TICKET_SECONDS
        )
        return value

    def consume(self, value: str) -> str:
        ticket = self.tickets.pop(hashlib.sha256(value.encode()).hexdigest(), None)
        if ticket is None or ticket.expires <= time.monotonic():
            raise HTTPException(401, "Invalid collaboration ticket")
        return ticket.token

    def add(self, token: str, actor: Actor, instance_id: str, protocol_version=1) -> Connection:
        if type(protocol_version) is not int or protocol_version not in (1, 2):
            raise ValueError("Unsupported presence protocol")
        key = actor.key(instance_id)
        if key in self.connections:
            raise FileExistsError("Presence instance already connected")
        if len(self.connections) >= MAX_CONNECTIONS:
            raise OverflowError("Collaboration connection capacity")
        connection = Connection(token, actor, instance_id, protocol_version=protocol_version)
        self.connections[key] = connection
        return connection

    def publish(self, connection: Connection, location: SurfaceLocation | None) -> None:
        if connection.closed or self.connections.get(connection.key) is not connection:
            return
        if connection.location == location:
            return
        previous = connection.location
        if previous:
            group = self.rooms.get(room_location(previous))
            if group is not None:
                group.discard(connection)
                if not group:
                    self.rooms.pop(room_location(previous), None)
            self.notify_room(previous)
        connection.location = location
        connection.image_state = None
        connection.image_embed = None
        connection.embed_revision += 1
        connection.base_state = None
        connection.base_revision += 1
        if location:
            self.rooms.setdefault(room_location(location), set()).add(connection)
            self.notify_room(location)
        connection.revision += 1
        self.notify(connection.key)

    def notify(self, target: PeerKey) -> None:
        for follower in self.followers.get(target, ()):
            follower.dirty.set()  # One replaceable latest state, never a movement queue.
        publisher = self.connections.get(target)
        if publisher and publisher.location:
            self.notify_room(publisher.location)

    def mark_watch(self, connection: Connection, watch: Watch, *, roster=True, states=True) -> None:
        if roster and not watch.embedded_images:
            watch.delivery.roster_pending = True
            connection.roster_dirty.set()
        if states and (watch.image_states or watch.embedded_images or watch.base_states):
            watch.delivery.states_pending = True
            connection.image_dirty.set()

    def notify_room(self, location: SurfaceLocation, *, roster=True) -> None:
        room = room_location(location)
        for viewer in self.watchers.get(room, ()):
            for watch in viewer.watches():
                if room_location(watch.location) == room:
                    self.mark_watch(viewer, watch, roster=roster)

    def notify_movement(self, publisher: Connection, location: SurfaceLocation) -> None:
        room = room_location(location)
        for viewer in self.watchers.get(room, ()):
            if viewer is publisher:
                continue
            for watch in viewer.watches():
                if room_location(watch.location) == room:
                    self.mark_watch(viewer, watch, roster=False)

    def move(
        self, connection: Connection, location: ImageLocation, state: ImageState | None
    ) -> None:
        if connection.closed or self.connections.get(connection.key) is not connection:
            return
        if connection.location != location:
            raise ValueError("Image movement requires matching visible publication")
        if connection.image_state == state:
            return
        connection.image_state = state
        # Movement never invalidates the participant roster or Follow identity.
        # Each observer holds one replaceable snapshot, not a movement queue.
        self.notify_movement(connection, location)

    def room_peers(self, location: SurfaceLocation, viewer: Connection) -> list[Connection]:
        return sorted(
            (peer for peer in self.rooms.get(room_location(location), ()) if peer is not viewer),
            key=lambda peer: peer.key,
        )

    def move_base(
        self, connection: Connection, location: BaseLocation, state: BaseState | None
    ) -> None:
        if connection.closed or self.connections.get(connection.key) is not connection:
            return
        if connection.location != location:
            raise ValueError("Bases position requires matching visible publication")
        if connection.base_state == state:
            return
        previous = connection.base_state
        if not previous or not state or previous.semantic_key() != state.semantic_key():
            connection.base_revision += 1
        connection.base_state = state
        self.notify_movement(connection, location)

    def move_embed(
        self, connection: Connection, location: Location, embed: EmbeddedImage | None
    ) -> None:
        if connection.closed or self.connections.get(connection.key) is not connection:
            return
        if not connection.location or not same_context(connection.location, location):
            raise ValueError("Embedded image requires matching visible note")
        if connection.image_embed == embed:
            return
        previous = connection.image_embed
        if not previous or not embed or previous.semantic_key() != embed.semantic_key():
            connection.embed_revision += 1
        connection.image_embed = embed
        self.notify_movement(connection, location)

    def _watch_rooms(self, connection: Connection) -> set[SurfaceLocation]:
        return {room_location(watch.location) for watch in connection.watches()}

    def _reindex_watches(self, connection: Connection, previous: set[SurfaceLocation]) -> None:
        current = self._watch_rooms(connection)
        for room in previous - current:
            group = self.watchers.get(room)
            if group is not None:
                group.discard(connection)
                if not group:
                    self.watchers.pop(room, None)
        for room in current - previous:
            self.watchers.setdefault(room, set()).add(connection)

    def watch(self, connection: Connection, watch: Watch | None) -> None:
        if watch and watch.subscription_id in connection.observations:
            raise ValueError("Duplicate observation identifier")
        previous = self._watch_rooms(connection)
        connection.watch = watch
        self._reindex_watches(connection, previous)
        if watch:
            self.mark_watch(connection, watch)

    def observe(self, connection: Connection, watch: Watch) -> None:
        if connection.watch and connection.watch.subscription_id == watch.subscription_id:
            raise ValueError("Duplicate observation identifier")
        if (
            watch.subscription_id not in connection.observations
            and len(connection.observations) >= MAX_OBSERVATIONS
        ):
            raise OverflowError("Too many presence observations")
        previous = self._watch_rooms(connection)
        connection.observations[watch.subscription_id] = watch
        self._reindex_watches(connection, previous)
        self.mark_watch(connection, watch)

    def unobserve(self, connection: Connection, subscription: str) -> None:
        previous = self._watch_rooms(connection)
        connection.observations.pop(subscription, None)
        self._reindex_watches(connection, previous)

    def remove_watch(self, connection: Connection, watch: Watch) -> None:
        if connection.watch is watch:
            self.watch(connection, None)
        elif connection.observations.get(watch.subscription_id) is watch:
            self.unobserve(connection, watch.subscription_id)

    def subscribe(self, connection: Connection, follow: Follow | None) -> None:
        if connection.follow:
            group = self.followers.get(connection.follow.target)
            if group is not None:
                group.discard(connection)
                if not group:
                    self.followers.pop(connection.follow.target, None)
        connection.follow = follow
        connection.last_location = None
        if follow:
            self.followers.setdefault(follow.target, set()).add(connection)
            connection.dirty.set()

    def remove(self, connection: Connection) -> None:
        self.publish(connection, None)
        connection.closed = True
        self.subscribe(connection, None)
        self.watch(connection, None)
        for subscription in tuple(connection.observations):
            self.unobserve(connection, subscription)
        if self.connections.get(connection.key) is connection:
            self.connections.pop(connection.key)
            self.notify(connection.key)

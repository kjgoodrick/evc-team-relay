"""Shared marker metadata and explicit saved-view placements, never local settings."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.timeline_ranges import TimelineRangeValue

MAX_REVISION = 9007199254740991


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimelineMarkerContent(StrictModel):
    kind: Literal["point", "range"]
    label: str = Field(min_length=1, max_length=256, strict=True)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$", strict=True)
    range: TimelineRangeValue

    @field_validator("label")
    @classmethod
    def valid_label(cls, value):
        if not value.strip() or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("A marker requires a nonempty plain-text label")
        return value

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value):
        return value.lower()

    @model_validator(mode="after")
    def point_has_one_coordinate(self):
        if self.kind == "point" and (
            self.range["start"] != self.range["end"]
            or not self.range["start_inclusive"]
            or not self.range["end_inclusive"]
        ):
            raise ValueError("A point marker requires identical closed endpoints")
        return self


class TimelineMarkerPlacement(StrictModel):
    view_id: uuid.UUID
    axis_definition_id: uuid.UUID
    axis_definition_revision: int = Field(ge=1, le=MAX_REVISION, strict=True)


class PutTimelineMarker(StrictModel):
    type: Literal["marker.put"]
    marker_id: uuid.UUID
    expected_marker_revision: int = Field(ge=0, lt=MAX_REVISION, strict=True)
    content: TimelineMarkerContent
    placements: list[TimelineMarkerPlacement] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_views(self):
        if len({item.view_id for item in self.placements}) != len(self.placements):
            raise ValueError("A marker can have only one placement in a saved view")
        return self


class ArchiveTimelineMarker(StrictModel):
    type: Literal["marker.archive"]
    marker_id: uuid.UUID
    expected_marker_revision: int = Field(ge=1, lt=MAX_REVISION, strict=True)


class TimelineMarkerCommand(StrictModel):
    operation_id: uuid.UUID
    definition_id: uuid.UUID
    generation: uuid.UUID
    registry_revision: int = Field(ge=1, le=MAX_REVISION, strict=True)
    expected_catalog_revision: int = Field(ge=0, lt=MAX_REVISION, strict=True)
    command: Annotated[PutTimelineMarker | ArchiveTimelineMarker, Field(discriminator="type")]


class TimelineMarkerCatalog(StrictModel):
    view_id: uuid.UUID | None = None
    include_archived: bool = Field(default=False, strict=True)
    after: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0, le=MAX_REVISION, strict=True)

    @model_validator(mode="after")
    def stable_page(self):
        if self.after is not None and self.expected_revision is None:
            raise ValueError("Continuing a marker catalog requires its revision")
        return self

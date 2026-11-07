import base64
import binascii
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.base_values import GroupValue


class CaptureBaseGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    definition_id: uuid.UUID
    view_id: uuid.UUID
    generation: uuid.UUID
    registry_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    group_definition_id: uuid.UUID
    group_definition_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    record_id: uuid.UUID
    record_generation: uuid.UUID
    dependency: str = Field(max_length=87384)
    time_zone: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_+/-]+$")
    value: GroupValue

    @field_validator("dependency")
    @classmethod
    def valid_dependency(cls, value):
        try:
            raw = base64.b64decode(value, validate=True)
            if not raw or len(raw) > 65536 or base64.b64encode(raw).decode() != value:
                raise ValueError("Invalid source dependency")
        except (ValueError, binascii.Error):
            raise ValueError("Invalid source dependency") from None
        return value


class InspectBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BaseTargetContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view_id: uuid.UUID
    record_id: uuid.UUID | None = None


class BaseTargetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1, le=9007199254740991, strict=True)
    after: uuid.UUID | None = None

    @model_validator(mode="after")
    def stable_page(self):
        if self.after is not None and self.expected_revision is None:
            raise ValueError("Continuing a catalog requires its registry revision")
        return self


class ViewBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: int = Field(ge=0, lt=64, strict=True)
    view_id: uuid.UUID | None


class ReconcileBase(InspectBase):
    model_config = ConfigDict(extra="forbid", strict=False)
    # UUID fields accept the JSON wire representation without weakening integers.
    operation_id: uuid.UUID = Field(strict=False)
    expected_revision: int = Field(ge=0, le=9007199254740990, strict=True)
    bindings: list[ViewBinding] = Field(max_length=64)
    archive: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        slots = [item.slot for item in self.bindings]
        ids = [item.view_id for item in self.bindings if item.view_id]
        if (
            len(set(slots)) != len(slots)
            or len(set(ids)) != len(ids)
            or len(set(self.archive)) != len(self.archive)
            or set(ids) & set(self.archive)
        ):
            raise ValueError("View assignments must be one-to-one")
        return self

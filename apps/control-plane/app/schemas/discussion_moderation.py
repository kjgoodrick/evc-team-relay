"""Explicit online administrative reads; never a queued discussion command."""

import uuid

from pydantic import Field, field_validator

from app.schemas.discussions import StrictModel


class ModerationReason(StrictModel):
    protocol_version: int = Field(default=1, ge=1, le=1, strict=True)
    reason: str = Field(min_length=1, max_length=512, strict=True)
    view_id: uuid.UUID | None = None

    @field_validator("reason")
    @classmethod
    def plain_reason(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("A plain-text reason is required")
        return value


class ModerationRead(ModerationReason):
    after: int = Field(default=0, ge=0, le=9007199254740991, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)

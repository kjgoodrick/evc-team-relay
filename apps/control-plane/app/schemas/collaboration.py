"""Versioned collaboration policy contract. Discussion APIs are not enabled yet."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class CommentRule(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    user_id: uuid.UUID
    path: str = Field(max_length=512)
    recursive: StrictBool = True
    allowed: StrictBool


class CommentRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, strict=True)
    rules: list[CommentRule] = Field(max_length=1000)


class CommentRuleSet(BaseModel):
    revision: int
    rules: list[CommentRule]


class EntryCapabilities(BaseModel):
    read: bool
    comment: bool
    edit: bool
    moderate: bool

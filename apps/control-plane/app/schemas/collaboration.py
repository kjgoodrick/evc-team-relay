"""Versioned collaboration capabilities and administrative permission contracts."""

import uuid
from typing import Literal

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


class PermissionFileRule(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    user_id: uuid.UUID
    path: str = Field(max_length=512)
    recursive: StrictBool = True
    permission: Literal["none", "read", "write"]


class PermissionMember(BaseModel):
    user_id: uuid.UUID
    email: str
    active: bool
    administrator: bool


class PermissionPolicy(BaseModel):
    version: Literal[1] = 1
    restore_guard: Literal[True] = True
    restore_generation: uuid.UUID | None = None
    share_id: uuid.UUID
    actor_id: uuid.UUID
    owner_id: uuid.UUID
    revision: str
    comment_revision: int
    file_rules: list[PermissionFileRule]
    comment_rules: list[CommentRule]
    members: list[PermissionMember]


class PermissionPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    actor_id: uuid.UUID
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_rules: list[PermissionFileRule] = Field(max_length=1000)
    comment_rules: list[CommentRule] = Field(max_length=1000)

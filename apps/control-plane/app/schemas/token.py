from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TokenMode(str, Enum):
    READ = "read"
    WRITE = "write"


class RelayTokenRequest(BaseModel):
    share_id: uuid.UUID
    doc_id: str = Field(min_length=1, max_length=512)  # matches relay_doc_bindings.doc_id
    mode: TokenMode = TokenMode.READ
    password: str | None = None
    file_path: str | None = None  # For folder shares: path of file within folder
    # Plugin manifest version (e.g. "0.0.9"), sent by clients new enough to know
    # this field exists. Optional and unvalidated against semver — older clients
    # simply omit it. Only signal available to tell which plugin version issued
    # a given token: User-Agent carries the Obsidian/Electron app version, not
    # the plugin's (#75491f2f recurrence diagnosis, 2026-09-17).
    client_version: str | None = Field(default=None, max_length=32)


class RelayTokenResponse(BaseModel):
    relay_url: str
    token: str
    expires_at: datetime
    restore_guard: bool | None = None
    restore_generation: uuid.UUID | None = None

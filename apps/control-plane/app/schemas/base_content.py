import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.services.managed_service import Record

MAX_BYTES = 256 * 1024
MAX_BASE64 = ((MAX_BYTES + 2) // 3) * 4


class BaseContentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=512)
    record: Record
    generation: uuid.UUID | None
    content_version: uuid.UUID | None


class PublishBaseContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    expected: BaseContentState
    content_base64: str = Field(max_length=MAX_BASE64)

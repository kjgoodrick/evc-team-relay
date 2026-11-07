import uuid
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from app.schemas.discussions import StrictModel


class DiscussionSearch(StrictModel):
    protocol_version: Literal[1] = 1
    query: str = Field(default="", max_length=256)
    state: Literal["all", "open", "resolved"] = "all"
    mine: StrictBool = False
    mentions: StrictBool = False
    entry_id: uuid.UUID | None = None
    view_id: uuid.UUID | None = None
    cursor: str | None = Field(default=None, max_length=2048)
    limit: StrictInt = Field(default=30, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Search must contain printable text")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Search must contain valid Unicode") from None
        return value.strip()

    @model_validator(mode="after")
    def saved_view_scope(self):
        if self.view_id is not None and self.entry_id is None:
            raise ValueError("A saved view requires its original file")
        return self

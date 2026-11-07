from typing import Literal

from pydantic import Field, StrictBool

from app.schemas.discussions import StrictModel


class SubscriptionUpdate(StrictModel):
    expected_version: int = Field(ge=0, le=9007199254740990, strict=True)
    following: StrictBool
    muted: StrictBool


class InboxUpdate(StrictModel):
    seen_version: int = Field(ge=1, le=9007199254740991, strict=True)
    action: Literal["read", "unread", "dismiss"]

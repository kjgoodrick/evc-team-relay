import uuid

from pydantic import Field

from app.schemas.discussions import StrictModel


class PictureStage(StrictModel):
    picture_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(strict=True, ge=1, le=10 * 1024 * 1024)

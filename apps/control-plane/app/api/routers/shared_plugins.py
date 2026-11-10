"""Small, authenticated setup manifest. Clients download pinned public releases.

Only owners/admins publish executable dependencies. Ordinary file editors cannot
change this resource. Configuration is opt-in and schema allowlisted, never a
copy of an arbitrary data.json file.
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import models as m
from app.db.session import get_db
from app.services import collaboration_auth, managed_service, share_service

router = APIRouter(prefix="/shared-plugins", tags=["shared plugin setup"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Asset(Strict):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(strict=True, ge=0, le=20000000)


class Dependency(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    name: str = Field(min_length=1, max_length=150)
    repository: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
    )
    tag: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
    version: str = Field(pattern=r"^[0-9][A-Za-z0-9._+-]{0,99}$")
    min_app_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    desktop_only: bool
    files: dict[str, Asset]
    settings: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_package(self):
        if self.id == "team-relay-sync":
            raise ValueError("Team Relay updates itself; it cannot be a managed dependency")
        if set(self.files) != {"main.js", "manifest.json", "styles.css"}:
            raise ValueError("Exactly the three runtime files are required")
        if (
            not 1 <= self.files["main.js"].size <= 20000000
            or not 1 <= self.files["manifest.json"].size <= 16384
            or self.files["styles.css"].size > 2000000
        ):
            raise ValueError("Invalid asset sizes")
        if self.settings:
            if (
                self.id != "timeline-gantt-view"
                or self.repository != "kjgoodrick/obsidian-time-and-line-planning"
            ):
                raise ValueError(
                    "This plugin has no portable-settings adapter; keep its settings local"
                )
            allowed = {
                "weekStart",
                "dependencyProperty",
                "lockedProperty",
                "showBarProperties",
                "barPropertyMinWidth",
            }
            if set(self.settings) - allowed:
                raise ValueError("Unknown or personal setting")
            for key, value in self.settings.items():
                if key == "weekStart" and (type(value) is not int or value not in [0, 1]):
                    raise ValueError("Invalid week start")
                if key == "showBarProperties" and type(value) is not bool:
                    raise ValueError("Invalid badge setting")
                if key == "barPropertyMinWidth" and (
                    type(value) is not int or not 0 <= value <= 1000
                ):
                    raise ValueError("Invalid badge width")
                if key in {"dependencyProperty", "lockedProperty"} and (
                    not isinstance(value, str) or not 1 <= len(value) <= 100
                ):
                    raise ValueError("Invalid property name")
        return self


class Publish(Strict):
    expected_revision: int = Field(strict=True, ge=0)
    plugins: list[Dependency] = Field(max_length=30)

    @model_validator(mode="after")
    def unique(self):
        if (
            len({p.id for p in self.plugins}) != len(self.plugins)
            or len(self.model_dump_json().encode()) > 131072
            or sum(a.size for p in self.plugins for a in p.files.values()) > 100000000
        ):
            raise ValueError("Duplicate plugins or oversized setup")
        return self


@router.get("/shares/{share_id}")
def read(
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: m.User = Depends(collaboration_auth.get_actor),
):
    share = share_service.get_share(db, share_id)
    managed_service.member(db, share, user)
    return {
        "revision": share.plugin_setup_revision,
        "plugins": share.plugin_setup,
        "can_manage": user.is_admin or share.owner_user_id == user.id,
    }


@router.put("/shares/{share_id}")
def publish(
    share_id: uuid.UUID,
    body: Publish,
    db: Session = Depends(get_db),
    user: m.User = Depends(collaboration_auth.get_actor),
):
    share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
    if share is None:
        raise HTTPException(404, "Share not found")
    managed_service.member(db, share, user)
    if not user.is_admin and share.owner_user_id != user.id:
        raise HTTPException(403, "Only the owner can publish a shared plugin setup")
    if share.plugin_setup_revision != body.expected_revision:
        raise HTTPException(409, "Setup changed. Reload before publishing.")
    share.plugin_setup = [p.model_dump() for p in body.plugins]
    share.plugin_setup_revision += 1
    db.commit()
    return {
        "revision": share.plugin_setup_revision,
        "plugins": share.plugin_setup,
        "can_manage": True,
    }

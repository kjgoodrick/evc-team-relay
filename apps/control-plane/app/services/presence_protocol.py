"""Bound and authenticate transient Yjs awareness at the managed gateway.

The relay remains the room authority and owns client-ID collision checks. Nothing
in this module writes a document, stores cursor history, or grants note access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models

MAX_FRAME = 16 * 1024 * 1024
MAX_AWARENESS = 4096
MAX_SAFE_INTEGER = 2**53 - 1
MAX_CLOCK = 2**31 - 1  # Leave room for relay-generated disconnect clocks.


class InvalidPresence(ValueError):
    pass


def varuint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def buffer(value: bytes) -> bytes:
    return varuint(len(value)) + value


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def integer(self, maximum=MAX_SAFE_INTEGER) -> int:
        value = 0
        for index in range(8):
            if self.pos >= len(self.data):
                raise InvalidPresence("Truncated integer")
            byte = self.data[self.pos]
            self.pos += 1
            value |= (byte & 127) << (7 * index)
            if value > maximum:
                raise InvalidPresence("Integer out of bounds")
            if byte < 128:
                if index and byte == 0:
                    raise InvalidPresence("Noncanonical integer")
                return value
        raise InvalidPresence("Integer out of bounds")

    def blob(self, maximum: int) -> bytes:
        length = self.integer(maximum)
        end = self.pos + length
        if end > len(self.data):
            raise InvalidPresence("Truncated buffer")
        result = self.data[self.pos : end]
        self.pos = end
        return result

    def finish(self):
        if self.pos != len(self.data):
            raise InvalidPresence("Trailing message data")


@dataclass(frozen=True)
class PresenceIdentity:
    user_id: str
    name: str
    session_id: str | None = None
    avatar_url: str | None = None

    def user(self) -> dict:
        result = {"id": self.user_id, "name": self.name}
        if self.avatar_url:
            result["avatarUrl"] = self.avatar_url
        return result


def identity_for(db: Session, user: models.User, claims: dict) -> PresenceIdentity:
    profile = db.scalar(
        select(models.UserOAuthAccount)
        .where(models.UserOAuthAccount.user_id == user.id)
        .order_by(models.UserOAuthAccount.updated_at.desc(), models.UserOAuthAccount.id)
        .limit(1)
    )
    name = (profile.name if profile else None) or user.email.split("@")[0]
    name = "".join(c for c in name if c.isprintable()).strip()[:255] or "Member"
    avatar = profile.picture_url if profile else None
    if avatar:
        try:
            parsed = urlsplit(avatar)
            if (
                len(avatar.encode("utf-8")) > 2048
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or not (
                    parsed.scheme == "https"
                    or parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                avatar = None
        except (ValueError, UnicodeError):
            avatar = None
    # Publish an opaque, stable session identity, never the login session UUID.
    session = claims.get("session_id")
    public_session = (
        hmac.new(
            get_settings().jwt_secret.encode(),
            f"presence:{user.id}:{session}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        if session
        else None
    )
    return PresenceIdentity(str(user.id), name, public_session, avatar)


def number(value, maximum=MAX_SAFE_INTEGER) -> bool:
    return type(value) is int and 0 <= value <= maximum


def relative_position(value) -> bool:
    if not isinstance(value, dict) or set(value) - {"type", "tname", "item", "assoc"}:
        return False
    if "assoc" in value and (type(value["assoc"]) is not int or value["assoc"] not in {-1, 0, 1}):
        return False
    if value.get("tname") != "contents" or value.get("type") is not None:
        return False
    item = value.get("item")
    return item is None or (
        isinstance(item, dict)
        and set(item) == {"client", "clock"}
        and number(item["client"])
        and number(item["clock"])
    )


def reject_constant(_):
    raise InvalidPresence("Invalid JSON number")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPresence("Duplicate JSON key")
        result[key] = value
    return result


class PresenceConnection:
    def __init__(self):
        self.client_id: int | None = None
        self.fallback_session = uuid.uuid4().hex

    def transform(self, frame: bytes, identity: PresenceIdentity | None) -> bytes:
        if not frame or len(frame) > MAX_FRAME:
            raise InvalidPresence("Invalid frame size")
        reader = Reader(frame)
        kind = reader.integer(255)
        if kind == 1:
            if identity is None:
                raise InvalidPresence("Missing authenticated identity")
            data = reader.blob(MAX_AWARENESS)
            reader.finish()
            return self.awareness(data, identity)
        if kind == 0:
            reader.integer(2)  # sync step 1, step 2 or update; relay enforces edit permission.
            reader.blob(MAX_FRAME)
        elif kind == 3:
            pass  # The server, never a client forwarding peers, answers initial awareness.
        elif kind in {5, 6, 7}:
            for _ in range(reader.integer(100)):
                reader.blob(1024)
        elif kind in {4, 102}:
            reader.blob(MAX_AWARENESS)
        else:
            raise InvalidPresence("Unsupported client message")
        reader.finish()  # No concatenated/trailing frame can bypass inspection.
        return frame

    def awareness(self, data: bytes, identity: PresenceIdentity) -> bytes:
        reader = Reader(data)
        if reader.integer(1) != 1:
            raise InvalidPresence("Send exactly one owned awareness state")
        client_id, clock = reader.integer(), reader.integer(MAX_CLOCK)
        raw = reader.blob(MAX_AWARENESS)
        reader.finish()
        if self.client_id is not None and self.client_id != client_id:
            raise InvalidPresence("Awareness identity changed")
        try:
            state = json.loads(
                raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_keys
            )
        except (ValueError, UnicodeError, RecursionError):
            raise InvalidPresence("Invalid awareness JSON") from None
        if state is None:
            if self.client_id is None:
                raise InvalidPresence("Unowned disconnect")
            encoded = b"null"
        else:
            if not isinstance(state, dict):
                raise InvalidPresence("Invalid awareness state")
            cursor = state.get("cursor")
            surface = state.get("surface")
            if surface is not None and surface not in ("editor", "reading"):
                raise InvalidPresence("Invalid presence surface")
            if cursor is not None and (
                not isinstance(cursor, dict)
                or set(cursor) != {"anchor", "head"}
                or not all(relative_position(cursor[key]) for key in ("anchor", "head"))
            ):
                raise InvalidPresence("Invalid cursor")
            if surface == "reading" and cursor is not None:
                raise InvalidPresence("Reading presence cannot publish an editor cursor")
            # Ignore all caller-supplied identity, colors, URLs and unknown state.
            # Each future surface extends this explicit schema, not arbitrary JSON.
            trusted = {
                "user": identity.user(),
                "sessionId": identity.session_id or self.fallback_session,
                "cursor": cursor,
            }
            if surface is not None:
                trusted["surface"] = surface
            encoded = json.dumps(trusted, separators=(",", ":"), ensure_ascii=False).encode()
            if len(encoded) > MAX_AWARENESS:
                raise InvalidPresence("Presence profile too large")
        payload = b"\x01" + varuint(client_id) + varuint(clock) + buffer(encoded)
        if len(payload) > MAX_AWARENESS:
            raise InvalidPresence("Presence payload too large")
        self.client_id = client_id
        return b"\x01" + buffer(payload)

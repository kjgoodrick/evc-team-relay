"""Bounded, process-local location subscriptions. No cursor history or activity feed.

All broker mutation runs on the collaboration socket's event loop. Database
checks use fresh sessions in worker threads. Deploy this opt-in channel to one
designated process until an interprocess ephemeral bus is implemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field

from app.api import deps
from app.core import security
from app.db.session import get_sessionmaker
from app.services import managed_service, share_service
from app.services.presence_protocol import PresenceIdentity, identity_for

MAX_CONNECTIONS = 128
MAX_TICKETS = 512
TICKET_SECONDS = 30
IDLE_SECONDS = 30
CHECK_SECONDS = 5
MAX_MESSAGE_BYTES = 4096
PeerKey = tuple[str, str, str]


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["note"]
    share_id: uuid.UUID
    entry_id: uuid.UUID
    surface: Literal["editor", "reading"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: uuid.UUID
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    instance_id: uuid.UUID

    def key(self) -> PeerKey:
        return str(self.user_id), self.session_id, str(self.instance_id)


@dataclass(frozen=True)
class Actor:
    identity: PresenceIdentity
    expires_at: int

    def key(self, instance_id: str) -> PeerKey:
        assert self.identity.session_id
        return self.identity.user_id, self.identity.session_id, instance_id


def authenticate(token: str, location: Location | None = None) -> Actor:
    with get_sessionmaker()() as db:
        user = deps._get_user_from_token(db, token)
        try:
            claims = security.decode_access_token(token)
        except InvalidTokenError:
            raise HTTPException(401, "Invalid or expired token") from None
        if not claims.get("session_id") or type(claims.get("exp")) is not int:
            raise HTTPException(401, "Session-backed credentials required")
        if location:
            share = share_service.get_share(db, location.share_id)
            entry = managed_service.authorize_entry(db, share, user, str(location.entry_id))
            if entry.record.get("type") != "markdown":
                raise HTTPException(403, "Location unavailable")
        return Actor(identity_for(db, user, claims), int(claims["exp"]))


def authorize_delivery(viewer_token: str, publisher_token: str, location: Location) -> None:
    # Both users must still have live sessions and read access to this exact
    # destination. A revoked publisher cannot keep an old location visible.
    authenticate(viewer_token, location)
    authenticate(publisher_token, location)


@dataclass
class Ticket:
    token: str
    user_id: str
    expires: float


@dataclass(frozen=True)
class Follow:
    subscription_id: str
    target: PeerKey


@dataclass(eq=False)
class Connection:
    token: str
    actor: Actor
    instance_id: str
    location: Location | None = None
    follow: Follow | None = None
    revision: int = 0
    closed: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    controls: list[dict] = field(default_factory=list)
    last_location: dict | None = None
    allowance: float = 20
    allowance_time: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> PeerKey:
        return self.actor.key(self.instance_id)

    def permit_message(self) -> bool:
        now = time.monotonic()
        self.allowance = min(20, self.allowance + (now - self.allowance_time) * 10)
        self.allowance_time = now
        if self.allowance < 1:
            return False
        self.allowance -= 1
        return True

    def control(self, message: dict) -> None:
        if len(self.controls) >= 16:
            raise OverflowError("Slow collaboration consumer")
        self.controls.append(message)
        self.dirty.set()


class PresenceBroker:
    def __init__(self):
        self.tickets: dict[str, Ticket] = {}
        self.connections: dict[PeerKey, Connection] = {}
        self.followers: dict[PeerKey, set[Connection]] = {}
        self.handshakes = 0

    def ticket(self, token: str, actor: Actor) -> str:
        now = time.monotonic()
        self.tickets = {key: item for key, item in self.tickets.items() if item.expires > now}
        if (
            len(self.tickets) >= MAX_TICKETS
            or sum(item.user_id == actor.identity.user_id for item in self.tickets.values()) >= 8
        ):
            raise HTTPException(429, "Too many pending collaboration tickets")
        value = secrets.token_urlsafe(32)
        self.tickets[hashlib.sha256(value.encode()).hexdigest()] = Ticket(
            token, actor.identity.user_id, now + TICKET_SECONDS
        )
        return value

    def consume(self, value: str) -> str:
        ticket = self.tickets.pop(hashlib.sha256(value.encode()).hexdigest(), None)
        if ticket is None or ticket.expires <= time.monotonic():
            raise HTTPException(401, "Invalid collaboration ticket")
        return ticket.token

    def add(self, token: str, actor: Actor, instance_id: str) -> Connection:
        key = actor.key(instance_id)
        if key in self.connections:
            raise FileExistsError("Presence instance already connected")
        if len(self.connections) >= MAX_CONNECTIONS:
            raise OverflowError("Collaboration connection capacity")
        connection = Connection(token, actor, instance_id)
        self.connections[key] = connection
        return connection

    def publish(self, connection: Connection, location: Location | None) -> None:
        if connection.closed or self.connections.get(connection.key) is not connection:
            return
        if connection.location == location:
            return
        connection.location = location
        connection.revision += 1
        self.notify(connection.key)

    def notify(self, target: PeerKey) -> None:
        for follower in self.followers.get(target, ()):
            follower.dirty.set()  # One replaceable latest state, never a movement queue.

    def subscribe(self, connection: Connection, follow: Follow | None) -> None:
        if connection.follow:
            group = self.followers.get(connection.follow.target)
            if group is not None:
                group.discard(connection)
                if not group:
                    self.followers.pop(connection.follow.target, None)
        connection.follow = follow
        connection.last_location = None
        if follow:
            self.followers.setdefault(follow.target, set()).add(connection)
            connection.dirty.set()

    def remove(self, connection: Connection) -> None:
        connection.closed = True
        self.subscribe(connection, None)
        if self.connections.get(connection.key) is connection:
            self.connections.pop(connection.key)
            self.notify(connection.key)

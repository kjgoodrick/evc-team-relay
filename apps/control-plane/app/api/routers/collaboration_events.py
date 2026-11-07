from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable

import anyio
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api import deps
from app.core.config import get_settings
from app.services import collaboration_presence as presence
from app.services.presence_protocol import InvalidPresence, reject_constant, unique_keys

router = APIRouter(prefix="/collaboration", tags=["collaboration"])


def enabled() -> bool:
    return get_settings().collaboration_presence_enabled


def broker(request: Request | WebSocket) -> presence.PresenceBroker:
    return request.app.state.collaboration_presence


@router.get("/image-location")
async def image_location(
    share_id: uuid.UUID,
    entry_id: uuid.UUID,
    response: Response,
    token: str | None = Depends(deps.oauth2_scheme),
):
    if not enabled():
        raise HTTPException(404, "Collaboration location channel disabled")
    if not token:
        raise HTTPException(401, "Missing credentials")
    location = await run_in_threadpool(presence.image_location, token, share_id, entry_id)
    response.headers["Cache-Control"] = "no-store"
    return {"protocol_version": 1, "location": location.model_dump(mode="json")}


@router.post("/socket-ticket")
async def socket_ticket(request: Request, token: str | None = Depends(deps.oauth2_scheme)):
    if not enabled():
        raise HTTPException(404, "Collaboration location channel disabled")
    if not token:
        raise HTTPException(401, "Missing credentials")
    actor = await run_in_threadpool(presence.authenticate, token)
    return {
        "protocol_version": 1,
        "ticket": broker(request).ticket(token, actor),
        "expires_in": presence.TICKET_SECONDS,
        "socket_path": "/v1/collaboration/events",
    }


async def receive(websocket: WebSocket) -> dict:
    raw = await websocket.receive_text()
    if len(raw.encode("utf-8")) > presence.MAX_MESSAGE_BYTES:
        raise ValueError("Message too large")
    result = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    if not isinstance(result, dict):
        raise ValueError("Expected message object")
    return result


def fields(message: dict, expected: set[str]) -> None:
    if set(message) != expected:
        raise ValueError("Invalid message fields")


def identifier(value) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid identifier")
    return value


def ticket_value(message: dict) -> str:
    value = message.get("ticket")
    if not isinstance(value, str) or not 32 <= len(value) <= 128:
        raise ValueError("Invalid ticket")
    return value


def authentication_version(message: dict, default=1) -> int:
    expected = {"type", "ticket", "instance_id"}
    if "protocol_version" in message:
        expected.add("protocol_version")
    fields(message, expected)
    version = message.get("protocol_version", default)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unsupported presence protocol")
    return version


async def handle(
    websocket: WebSocket, owner: presence.PresenceBroker, connection: presence.Connection
):
    while True:
        message = await receive(websocket)
        if not connection.permit_message():
            raise OverflowError("Collaboration message rate exceeded")
        kind = message.get("type")
        if kind == "authenticate":
            if (
                authentication_version(message, connection.protocol_version)
                != connection.protocol_version
            ):
                raise ValueError("Cannot switch presence protocols")
            if identifier(message["instance_id"]) != connection.instance_id:
                raise ValueError("Cannot switch instances")
            token = owner.consume(ticket_value(message))
            actor = await run_in_threadpool(presence.authenticate, token)
            if actor.key(connection.instance_id) != connection.key:
                raise HTTPException(401, "Cannot switch accounts or login sessions")
            connection.token, connection.actor = token, actor
            connection.revision += 1
            owner.notify(connection.key)
            connection.control({"type": "authenticated", "expires_at": actor.expires_at})
        else:
            # Refresh the identity/session even for unpublish and subscription
            # control frames; possession of an old socket is not authorization.
            image_location, image_state = None, None
            if kind == "image_state" and connection.protocol_version == 2:
                fields(message, {"type", "location", "state"})
                image_location = presence.ImageLocation.model_validate(message["location"])
                image_state = (
                    presence.ImageState.model_validate(message["state"])
                    if message["state"] is not None
                    else None
                )
            # A movement frame performs identity and entry authorization in one
            # fresh database session rather than repeating session lookup.
            actor = await run_in_threadpool(presence.authenticate, connection.token, image_location)
            connection.actor = actor
            if kind == "presence":
                fields(message, {"type", "location"})
                location = (
                    presence.parse_location(message["location"], connection.protocol_version)
                    if message["location"] is not None
                    else None
                )
                if location:
                    await run_in_threadpool(presence.authenticate, connection.token, location)
                owner.publish(connection, location)
            elif kind == "image_state" and connection.protocol_version == 2:
                owner.move(connection, image_location, image_state)
            elif kind == "follow":
                fields(message, {"type", "subscription_id", "target", "from"})
                subscription = identifier(message["subscription_id"])
                target = presence.Target.model_validate(message["target"]).key()
                source = presence.parse_location(message["from"], connection.protocol_version)
                publisher = owner.connections.get(target)
                revision = publisher.revision if publisher else -1
                # Initial consent is scoped to a shared context already visible
                # to both parties, not a user-ID search across all active notes.
                available = (
                    publisher
                    and publisher is not connection
                    and publisher.location
                    and presence.supports_location(connection.protocol_version, publisher.location)
                    and presence.same_context(publisher.location, source)
                )
                if available:
                    try:
                        await run_in_threadpool(
                            presence.authorize_delivery,
                            connection.token,
                            publisher.token,
                            publisher.location,
                        )
                    except HTTPException:
                        available = False
                if (
                    available
                    and owner.connections.get(target) is publisher
                    and publisher.revision == revision
                ):
                    owner.subscribe(connection, presence.Follow(subscription, target))
                    connection.control({"type": "following", "subscription_id": subscription})
                else:
                    owner.subscribe(connection, None)
                    connection.control(
                        {
                            "type": "location",
                            "subscription_id": subscription,
                            "status": "unavailable",
                        }
                    )
            elif kind == "unfollow":
                fields(message, {"type", "subscription_id"})
                subscription = identifier(message["subscription_id"])
                if connection.follow and connection.follow.subscription_id == subscription:
                    owner.subscribe(connection, None)
            elif kind == "watch" and connection.protocol_version == 2:
                fields(
                    message,
                    {"type", "subscription_id", "location"}
                    | ({"image_states"} if "image_states" in message else set()),
                )
                subscription = identifier(message["subscription_id"])
                location = presence.parse_location(message["location"], 2)
                if isinstance(location, presence.Location):
                    raise ValueError("Notes use managed awareness")
                image_states = message.get("image_states", False)
                if (
                    type(image_states) is not bool
                    or image_states
                    and not isinstance(location, presence.ImageLocation)
                ):
                    raise ValueError("Invalid image observation")
                await run_in_threadpool(presence.authenticate, connection.token, location)
                owner.watch(connection, presence.Watch(subscription, location, image_states))
            elif kind == "unwatch" and connection.protocol_version == 2:
                fields(message, {"type", "subscription_id"})
                subscription = identifier(message["subscription_id"])
                if connection.watch and connection.watch.subscription_id == subscription:
                    owner.watch(connection, None)
            elif kind == "ping":
                fields(message, {"type"})
                connection.control({"type": "pong"})
            else:
                raise ValueError("Unknown message")
        connection.last_seen = time.monotonic()


async def send(
    websocket: WebSocket,
    connection: presence.Connection,
    message: dict,
    current: Callable[[], bool] | None = None,
) -> bool:
    # Roster and Follow have independent latest-state work, but serialize writes
    # to the one socket and bound the combined wait/send time.
    async with asyncio.timeout(5):
        async with connection.send_lock:
            if current and not current():
                return False
            await websocket.send_json(message)
            return True


async def deliver(
    websocket: WebSocket, owner: presence.PresenceBroker, viewer: presence.Connection
):
    while True:
        await viewer.dirty.wait()
        viewer.dirty.clear()
        while viewer.controls:
            await send(websocket, viewer, viewer.controls.pop(0))
        follow = viewer.follow
        if not follow:
            continue
        publisher = owner.connections.get(follow.target)
        revision = publisher.revision if publisher else -1
        location = publisher.location if publisher else None
        allowed = False
        viewer_token = viewer.token
        if publisher and location and presence.supports_location(viewer.protocol_version, location):
            try:
                await run_in_threadpool(
                    presence.authorize_delivery, viewer_token, publisher.token, location
                )
                allowed = True
            except HTTPException:
                pass
        if viewer.closed or viewer.follow != follow:
            continue
        if (
            viewer.token != viewer_token
            or owner.connections.get(follow.target) is not publisher
            or publisher
            and publisher.revision != revision
        ):
            viewer.dirty.set()
            continue  # The checked snapshot changed while authorization awaited.
        result = {
            "type": "location",
            "subscription_id": follow.subscription_id,
            "status": "unavailable",
        }
        if allowed:
            result.update(status="available", location=location.model_dump(mode="json"))
        if result != viewer.last_location:

            def current():
                return (
                    not viewer.closed
                    and viewer.follow == follow
                    and viewer.token == viewer_token
                    and owner.connections.get(follow.target) is publisher
                    and (not publisher or publisher.revision == revision)
                )

            if not await send(websocket, viewer, result, current):
                viewer.dirty.set()
                continue
            if viewer.follow == follow:
                viewer.last_location = result
        if not allowed and viewer.follow == follow:
            owner.subscribe(viewer, None)  # No silent resumption after access/visibility loss.


async def deliver_roster(
    websocket: WebSocket,
    owner: presence.PresenceBroker,
    viewer: presence.Connection,
    image_states: bool = False,
):
    dirty = viewer.image_dirty if image_states else viewer.roster_dirty
    while True:
        await dirty.wait()
        if image_states:
            await asyncio.sleep(presence.IMAGE_STATE_INTERVAL)
        dirty.clear()
        watch, token = viewer.watch, viewer.token
        if not watch or image_states and not watch.image_states:
            continue
        peers = owner.room_peers(watch.location, viewer)
        captured = [(peer, peer.revision, peer.token) for peer in peers]
        result = {
            "type": "image_states" if image_states else "roster",
            "subscription_id": watch.subscription_id,
            "status": "unavailable",
        }
        try:
            identities = await run_in_threadpool(
                presence.roster_snapshot,
                token,
                watch.location,
                [(peer.key, saved_token) for peer, _, saved_token in captured],
            )
            if image_states:
                authorized = {
                    tuple(item["target"][key] for key in ("user_id", "session_id", "instance_id"))
                    for item in identities
                }
                # Read the latest movement after authorization; movement itself
                # does not invalidate authorization or starve slow lookups.
                identities = [
                    {
                        "target": {
                            "user_id": peer.key[0],
                            "session_id": peer.key[1],
                            "instance_id": peer.key[2],
                        },
                        "state": peer.image_state.model_dump(mode="json"),
                    }
                    for peer in peers
                    if peer.key in authorized and peer.image_state is not None
                ]
            result.update(status="available", peers=identities)
        except HTTPException:
            pass
        if viewer.closed or viewer.watch != watch:
            continue
        if (
            viewer.token != token
            or peers != owner.room_peers(watch.location, viewer)
            or any(
                peer.revision != revision or peer.token != saved_token
                for peer, revision, saved_token in captured
            )
        ):
            dirty.set()
            continue
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > presence.MAX_ROSTER_BYTES:
            raise OverflowError("Presence roster too large")
        if result != (viewer.last_image_states if image_states else viewer.last_roster):

            def current():
                return (
                    not viewer.closed
                    and viewer.watch == watch
                    and viewer.token == token
                    and peers == owner.room_peers(watch.location, viewer)
                    and all(
                        peer.revision == revision and peer.token == saved_token
                        for peer, revision, saved_token in captured
                    )
                )

            if not await send(websocket, viewer, result, current):
                dirty.set()
                continue
            if viewer.watch == watch:
                if image_states:
                    viewer.last_image_states = result
                else:
                    viewer.last_roster = result
        if result["status"] == "unavailable" and viewer.watch == watch:
            owner.watch(viewer, None)


async def check_live(owner: presence.PresenceBroker, connection: presence.Connection):
    while True:
        await asyncio.sleep(presence.CHECK_SECONDS)
        if not enabled() or time.monotonic() - connection.last_seen > presence.IDLE_SECONDS:
            raise HTTPException(401, "Collaboration connection expired")
        token = connection.token
        try:
            actor = await run_in_threadpool(presence.authenticate, token)
        except HTTPException:
            if connection.token != token:
                continue
            raise
        if connection.token != token:
            continue
        connection.actor = actor
        location, revision = connection.location, connection.revision
        if location:
            try:
                await run_in_threadpool(presence.authenticate, token, location)
            except HTTPException:
                if connection.token == token and connection.revision == revision:
                    owner.publish(connection, None)
        if connection.follow:
            connection.dirty.set()  # Recheck destination access even when both peers idle.
        if connection.watch:
            connection.roster_dirty.set()
            if connection.watch.image_states:
                connection.image_dirty.set()


@router.websocket("/events")
async def events(websocket: WebSocket):
    owner = broker(websocket)
    if (
        not enabled()
        or websocket.query_params
        or len(owner.connections) + owner.handshakes >= presence.MAX_CONNECTIONS
    ):
        await websocket.close(code=4403 if not enabled() or websocket.query_params else 4429)
        return
    owner.handshakes += 1
    pending_handshake = True
    connection = None
    tasks: list[asyncio.Task] = []
    code = 1000
    try:
        await websocket.accept()
        message = await asyncio.wait_for(receive(websocket), timeout=5)
        protocol_version = authentication_version(message)
        if message["type"] != "authenticate":
            raise ValueError("Authenticate first")
        instance = identifier(message["instance_id"])
        token = owner.consume(ticket_value(message))
        actor = await run_in_threadpool(presence.authenticate, token)
        connection = owner.add(token, actor, instance, protocol_version)
        owner.handshakes -= 1
        pending_handshake = False
        connection.control(
            {
                "type": "ready",
                "protocol_version": protocol_version,
                "session_id": actor.identity.session_id,
                "instance_id": instance,
                "expires_at": actor.expires_at,
            }
        )
        tasks = [
            asyncio.create_task(handle(websocket, owner, connection)),
            asyncio.create_task(deliver(websocket, owner, connection)),
            asyncio.create_task(deliver_roster(websocket, owner, connection)),
            asyncio.create_task(deliver_roster(websocket, owner, connection, True)),
            asyncio.create_task(check_live(owner, connection)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except HTTPException:
        code = 4403
    except FileExistsError:
        code = 4409
    except OverflowError:
        code = 4429
    except (ValueError, ValidationError, InvalidPresence, KeyError, UnicodeError):
        code = 4400
    except TimeoutError:
        code = 4408
    finally:
        if pending_handshake:
            owner.handshakes -= 1
        if connection:
            owner.remove(connection)
        # ASGI shutdown can cancel the request while worker tasks are waiting
        # for a frame or database result. Finish their cleanup within the
        # request's lifetime, including under an AnyIO cancellation scope.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=code)

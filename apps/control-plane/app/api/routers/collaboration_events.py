from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
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


async def handle(
    websocket: WebSocket, owner: presence.PresenceBroker, connection: presence.Connection
):
    while True:
        message = await receive(websocket)
        if not connection.permit_message():
            raise OverflowError("Collaboration message rate exceeded")
        kind = message.get("type")
        if kind == "authenticate":
            fields(message, {"type", "ticket", "instance_id"})
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
            actor = await run_in_threadpool(presence.authenticate, connection.token)
            connection.actor = actor
            if kind == "presence":
                fields(message, {"type", "location"})
                location = (
                    presence.Location.model_validate(message["location"])
                    if message["location"] is not None
                    else None
                )
                if location:
                    await run_in_threadpool(presence.authenticate, connection.token, location)
                owner.publish(connection, location)
            elif kind == "follow":
                fields(message, {"type", "subscription_id", "target", "from"})
                subscription = identifier(message["subscription_id"])
                target = presence.Target.model_validate(message["target"]).key()
                source = presence.Location.model_validate(message["from"])
                publisher = owner.connections.get(target)
                revision = publisher.revision if publisher else -1
                # Initial consent is scoped to a shared context already visible
                # to both parties, not a user-ID search across all active notes.
                available = (
                    publisher
                    and publisher is not connection
                    and publisher.location
                    and (
                        publisher.location.share_id == source.share_id
                        and publisher.location.entry_id == source.entry_id
                    )
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
            elif kind == "ping":
                fields(message, {"type"})
                connection.control({"type": "pong"})
            else:
                raise ValueError("Unknown message")
        connection.last_seen = time.monotonic()


async def deliver(
    websocket: WebSocket, owner: presence.PresenceBroker, viewer: presence.Connection
):
    while True:
        await viewer.dirty.wait()
        viewer.dirty.clear()
        while viewer.controls:
            await asyncio.wait_for(websocket.send_json(viewer.controls.pop(0)), timeout=5)
        follow = viewer.follow
        if not follow:
            continue
        publisher = owner.connections.get(follow.target)
        revision = publisher.revision if publisher else -1
        location = publisher.location if publisher else None
        allowed = False
        viewer_token = viewer.token
        if publisher and location:
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
            await asyncio.wait_for(websocket.send_json(result), timeout=5)
            viewer.last_location = result
        if not allowed:
            owner.subscribe(viewer, None)  # No silent resumption after access/visibility loss.


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
        fields(message, {"type", "ticket", "instance_id"})
        if message["type"] != "authenticate":
            raise ValueError("Authenticate first")
        instance = identifier(message["instance_id"])
        token = owner.consume(ticket_value(message))
        actor = await run_in_threadpool(presence.authenticate, token)
        connection = owner.add(token, actor, instance)
        owner.handshakes -= 1
        pending_handshake = False
        connection.control(
            {
                "type": "ready",
                "protocol_version": 1,
                "session_id": actor.identity.session_id,
                "instance_id": instance,
                "expires_at": actor.expires_at,
            }
        )
        tasks = [
            asyncio.create_task(handle(websocket, owner, connection)),
            asyncio.create_task(deliver(websocket, owner, connection)),
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

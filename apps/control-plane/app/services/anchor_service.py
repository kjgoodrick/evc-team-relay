"""Authenticated private relay observations. No SQL or public bearer-proof input."""

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from app.core.config import get_settings
from app.schemas.discussions import CommandEnvelope, ThreadCreate, source_anchor

_slots = threading.BoundedSemaphore(4)


def now_ms() -> int:
    return int(time.time() * 1000)


def enabled() -> bool:
    settings = get_settings()
    return bool(
        settings.collaboration_policy_enabled
        and settings.collaboration_discussions_enabled
        and settings.collaboration_text_anchors_enabled
        and settings.collaboration_anchor_relay_token
        and settings.collaboration_anchor_relay_token.get_secret_value()
    )


def fail(status=503, code="anchor_validation_unavailable"):
    raise HTTPException(
        status,
        {"code": code},
        headers={"Retry-After": "2"}
        if status == 503 or code == "anchor_dependency_not_ready"
        else None,
    ) from None


def command_hash(envelope: CommandEnvelope) -> str:
    return hashlib.sha256(envelope.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AnchorProof:
    command_sha256: str
    entry_id: uuid.UUID
    share_id: uuid.UUID
    generation: uuid.UUID
    expires_at_ms: int
    deadline: float
    observed_snapshot_sha256: str
    placement: str

    def check(self, envelope: CommandEnvelope, generation: uuid.UUID | None):
        if (
            self.command_sha256 != command_hash(envelope)
            or self.entry_id != envelope.entry_id
            or self.share_id != envelope.share_id
            or self.generation != generation
        ):
            fail(409, "anchor_generation_mismatch")
        if now_ms() >= self.expires_at_ms or time.monotonic() >= self.deadline:
            fail(503, "anchor_validation_expired")


def relay_origin() -> str:
    try:
        parsed = urlsplit(get_settings().managed_relay_url)
        # Force validation of bracketed hosts and ports before building a URL.
        parsed.port
    except ValueError:
        fail()
    if (
        parsed.scheme not in {"ws", "wss", "http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        fail()
    return urlunsplit(
        ("https" if parsed.scheme in {"wss", "https"} else "http", parsed.netloc, "", "", "")
    )


def _post(client, origin, token, route, request, deadline):
    encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode()
    sent = now_ms()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        fail(503, "anchor_validation_expired")
    try:
        with client.stream(
            "POST",
            origin + "/internal/collaboration/" + route,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            content=encoded,
            timeout=min(8.0, remaining),
        ) as response:
            body = bytearray()
            for chunk in response.iter_raw():
                if time.monotonic() >= deadline or len(body) + len(chunk) > 65536:
                    fail()
                body.extend(chunk)
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                fail()
            if not isinstance(value, dict):
                fail()
            if response.status_code != 200:
                code = value.get("code")
                if response.status_code == 409 and code in {
                    "generation_mismatch",
                    "share_mismatch",
                }:
                    fail(409, "anchor_generation_mismatch")
                if (response.status_code == 409 and code == "dependency_not_ready") or (
                    response.status_code == 404 and code == "document_unavailable"
                ):
                    fail(409, "anchor_dependency_not_ready")
                if response.status_code == 422:
                    fail(422, "invalid_anchor")
                fail()
    except httpx.HTTPError:
        # Network exceptions can include URLs/headers. Do not propagate them to
        # the application logger or expose private transport configuration.
        fail()
    expected = {
        key: request[key]
        for key in (
            "protocol_version",
            "request_id",
            "document_id",
            "share_id",
            "generation",
            "expires_at_ms",
        )
    }
    if (
        any(value.get(key) != item for key, item in expected.items())
        or type(value.get("protocol_version")) is not int
        or type(value.get("expires_at_ms")) is not int
        or value.get("request_sha256") != hashlib.sha256(encoded).hexdigest()
        or type(value.get("checkpointed_at_ms")) is not int
        or not sent - 1000 <= value["checkpointed_at_ms"] <= now_ms() + 1000
        or value["expires_at_ms"] <= now_ms()
        or time.monotonic() >= deadline
    ):
        fail()
    return value


def validate(envelope: CommandEnvelope) -> AnchorProof:
    if not enabled():
        fail(409, "text_anchors_disabled")
    if not isinstance(envelope.command, ThreadCreate):
        fail(422, "invalid_anchor")
    anchor = source_anchor(envelope.command.anchor)
    if anchor is None or anchor.document_id != envelope.entry_id:
        fail(422, "invalid_anchor")
    if not _slots.acquire(blocking=False):
        fail()
    try:
        deadline = time.monotonic() + 12
        request = {
            "protocol_version": 1,
            "request_id": str(uuid.uuid4()),
            "document_id": str(envelope.entry_id),
            "share_id": str(envelope.share_id),
            "generation": str(anchor.generation),
            "expires_at_ms": now_ms() + 15000,
        }
        origin = relay_origin()
        token = get_settings().collaboration_anchor_relay_token.get_secret_value()
        # No environment proxy or redirect may forward the private credential.
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            _post(client, origin, token, "register", request, deadline)
            request = {
                **request,
                "request_id": str(uuid.uuid4()),
                "anchors": [
                    {
                        "dependency": anchor.dependency,
                        "start": anchor.start,
                        "end": anchor.end,
                        "selected_length": anchor.selected_length,
                    }
                ],
            }
            response = _post(client, origin, token, "validate", request, deadline)
        snapshot = response.get("observed_snapshot_sha256")
        placements = response.get("placements")
        if (
            not isinstance(snapshot, str)
            or len(snapshot) != 64
            or any(char not in "0123456789abcdef" for char in snapshot)
            or not isinstance(placements, list)
            or len(placements) != 1
            or not isinstance(placements[0], dict)
        ):
            fail()
        placement = placements[0]
        if placement.get("status") == "attached":
            if (
                type(placement.get("from")) is not int
                or type(placement.get("to")) is not int
                or not 0 <= placement["from"] <= placement["to"] <= 4294967295
                or (anchor.selected_length > 0 and placement["from"] == placement["to"])
            ):
                fail()
        elif placement.get("status") != "unanchored":
            fail()
        return AnchorProof(
            command_hash(envelope),
            envelope.entry_id,
            envelope.share_id,
            anchor.generation,
            request["expires_at_ms"],
            deadline,
            snapshot,
            placement["status"],
        )
    finally:
        _slots.release()

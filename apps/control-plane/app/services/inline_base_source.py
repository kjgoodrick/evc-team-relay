"""Read inline Base configuration from a private, checkpointed relay observation.

Callers authorize the containing note before I/O and recheck access/generation
under the share lock before consuming the result. Client-supplied YAML is never
accepted. This module does not grant access or persist a registry identity.
"""

import base64
import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass

import httpx
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.services import anchor_service, bases_definition

_slots = threading.BoundedSemaphore(2)
MAX_SOURCE_BYTES = 64 * 1024


class InlineSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dependency: str = Field(max_length=87384)
    start: str = Field(max_length=344)
    end: str = Field(max_length=344)
    selected_length: int = Field(ge=4, le=256, strict=True)

    @field_validator("dependency", "start", "end")
    @classmethod
    def encoded_bytes(cls, value, info):
        try:
            raw = base64.b64decode(value, validate=True)
            limit = 65536 if info.field_name == "dependency" else 256
            if not raw or len(raw) > limit or base64.b64encode(raw).decode() != value:
                raise ValueError()
        except ValueError:
            raise ValueError("Invalid inline source anchor") from None
        return value

    def identity(self, share_id, entry_id, generation):
        # Snapshot dependencies change as the note is edited. Only the fence's
        # stable CRDT positions participate in definition identity.
        return bases_definition.fingerprint(
            [str(share_id), str(entry_id), str(generation), self.start, self.end]
        )


def definition_at(source: str, start: int, end: int):
    """Find a real closed fence at exact UTF-16 header offsets, including quotes.

    Markdown parsing excludes fence-looking examples inside other code blocks.
    Source offsets are checked against the opening line rather than a body hash,
    so identical definitions do not inherit each other's identity.
    """
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        anchor_service.fail(422, "invalid_inline_base")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        anchor_service.fail(413, "inline_base_source_too_large")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-16-le")) // 2)
    frontmatter = re.match(
        r"^(?:\ufeff)?---[ \t]*\r?\n(?:[\s\S]*?\r?\n)?(?:---|\.\.\.)[ \t]*(?=\r?\n|$)", source
    )
    frontmatter_end = len(frontmatter[0].encode("utf-16-le")) // 2 if frontmatter else 0
    for token in MarkdownIt("commonmark").parse(source):
        if token.type != "fence" or token.info.strip() != "base" or not token.map:
            continue
        first, after = token.map
        if first >= len(lines) or after <= first + 1 or offsets[first] < frontmatter_end:
            continue
        line = lines[first].rstrip("\r\n")
        # The parser identifies the fence; exact CRDT offsets identify this
        # occurrence. Leading list/quote markers are not part of the anchor.
        match = re.search(re.escape(token.markup) + r"[ \t]*base[ \t]*$", line)
        if not match:
            continue
        header_start = offsets[first] + len(line[: match.start()].encode("utf-16-le")) // 2
        header_end = offsets[first] + len(line.rstrip().encode("utf-16-le")) // 2
        closing = lines[after - 1].rstrip("\r\n")
        if not re.search(
            re.escape(token.markup[0]) + "{" + str(len(token.markup)) + r",}[ \t]*$", closing
        ):
            continue
        if (start, end) != (header_start, header_end):
            continue
        # Both native and server parsers omit the terminating line break.
        body = token.content.removesuffix("\n")
        try:
            return bases_definition.parse(body.encode("utf-8"))
        except bases_definition.InvalidBase:
            anchor_service.fail(422, "invalid_inline_base")
    anchor_service.fail(409, "inline_base_detached")


@dataclass(frozen=True)
class InlineSourceObservation:
    identity_sha256: str
    snapshot_sha256: str
    definition: bases_definition.Definition
    expires_at_ms: int
    deadline: float

    def check(self, share_id, entry_id, generation, request):
        if self.identity_sha256 != request.identity(share_id, entry_id, generation):
            anchor_service.fail(409, "anchor_generation_mismatch")
        if anchor_service.now_ms() >= self.expires_at_ms or time.monotonic() >= self.deadline:
            anchor_service.fail(503, "anchor_validation_expired")


def observe(
    share_id: uuid.UUID, entry_id: uuid.UUID, generation: uuid.UUID, request: InlineSourceRequest
):
    if not anchor_service.enabled():
        anchor_service.fail(409, "text_anchors_disabled")
    if not _slots.acquire(blocking=False):
        anchor_service.fail(503, "inline_base_source_busy")
    try:
        deadline = time.monotonic() + 12
        envelope = dict(
            protocol_version=1,
            request_id=str(uuid.uuid4()),
            document_id=str(entry_id),
            share_id=str(share_id),
            generation=str(generation),
            expires_at_ms=anchor_service.now_ms() + 15000,
        )
        origin = anchor_service.relay_origin()
        token = get_settings().collaboration_anchor_relay_token.get_secret_value()
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            anchor_service._post(client, origin, token, "register", envelope, deadline)
            placement = anchor_service._post(
                client,
                origin,
                token,
                "validate",
                {
                    **envelope,
                    "request_id": str(uuid.uuid4()),
                    "anchors": [request.model_dump()],
                },
                deadline,
            )
            source = anchor_service._post(
                client,
                origin,
                token,
                "source",
                {
                    **envelope,
                    "request_id": str(uuid.uuid4()),
                    "source_dependency": request.dependency,
                },
                deadline,
                response_limit=512 * 1024,
            )
        snapshot = source.get("observed_snapshot_sha256")
        if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot):
            anchor_service.fail()
        if placement.get("observed_snapshot_sha256") != snapshot:
            anchor_service.fail(409, "inline_base_source_changed")
        prefix = source.get("source_prefix")
        if not isinstance(prefix, str) or len(prefix.encode("utf-8")) > MAX_SOURCE_BYTES:
            anchor_service.fail()
        if source.get("source_complete") is not True:
            anchor_service.fail(413, "inline_base_source_too_large")
        if hashlib.sha256(prefix.encode()).hexdigest() != source.get("source_prefix_sha256"):
            anchor_service.fail()
        positions = placement.get("placements")
        if (
            not isinstance(positions, list)
            or len(positions) != 1
            or not isinstance(positions[0], dict)
        ):
            anchor_service.fail()
        position = positions[0]
        if position.get("status") != "attached":
            anchor_service.fail(409, "inline_base_detached")
        definition = definition_at(prefix, position.get("from"), position.get("to"))
        return InlineSourceObservation(
            request.identity(share_id, entry_id, generation),
            snapshot,
            definition,
            envelope["expires_at_ms"],
            deadline,
        )
    finally:
        _slots.release()


class InspectInlineSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: uuid.UUID
    source: InlineSourceRequest


def inspect(db, token, share_id, entry_id, payload: InspectInlineSource, *, persist=False):
    """Inspect verified source, optionally persisting its definition/view registry.

    Inspection requires read access and never changes the containing note.
    """
    from app.services import collaboration_restore, discussion_service

    def context(write=False):
        return discussion_service.context(
            db,
            token,
            share_id,
            entry_id,
            write=write,
            entry_types={"markdown"},
            require_comment=False,
        )

    try:
        ctx = context()
        if ctx.entry.collaboration_generation != payload.generation:
            anchor_service.fail(409, "anchor_generation_mismatch")
        restore = ctx.share.collaboration_restore_generation
        expected_revision = 0
        if persist:
            from app.services.inline_bases_registry import revision_for

            expected_revision = revision_for(
                db, ctx.entry.id, payload.source.identity(share_id, entry_id, payload.generation)
            )
        db.commit()
        proof = observe(share_id, entry_id, payload.generation, payload.source)
        ctx = context(write=persist)
        collaboration_restore.require_generation(ctx.share, restore)
        if ctx.entry.collaboration_generation != payload.generation:
            anchor_service.fail(409, "anchor_generation_mismatch")
        proof.check(share_id, entry_id, payload.generation, payload.source)
        if persist:
            from app.services.inline_bases_registry import synchronize_verified

            result = synchronize_verified(db, ctx, proof, payload.source, expected_revision)
            db.commit()
            return result
        result = {
            "protocol_version": 1,
            "entry_id": str(entry_id),
            "generation": str(payload.generation),
            "source_identity_sha256": proof.identity_sha256,
            "content_sha256": proof.definition.sha256,
            "configuration": proof.definition.value,
        }
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise

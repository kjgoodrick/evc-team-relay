"""Private relay observations for subsequent server-derived Bases value proofs.

This is not a public endpoint or a durable group-value proof. Callers must check
the actor's base/source access before I/O, release SQL during I/O, then recheck
current permissions, record lineage and grouping definition while holding the
share lock before consuming the observation. No client-provided source body or
dependency *list* is accepted. The snapshot dependency is verified by the relay.
"""

import base64
import binascii
import hashlib
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import cached_property

import httpx
import yaml

from app.core.config import get_settings
from app.services import anchor_service, base_discussions, bases_definition

MAX_PREFIX_BYTES = 64 * 1024
_slots = threading.BoundedSemaphore(2)
_hash = re.compile(r"^[0-9a-f]{64}$")


def enabled():
    token = get_settings().collaboration_anchor_relay_token
    return bool(base_discussions.definitions_enabled() and token and token.get_secret_value())


def source_identity(share_id, entry_id, generation, dependency):
    if not all(isinstance(value, uuid.UUID) for value in (share_id, entry_id, generation)):
        anchor_service.fail(422, "invalid_base_value_source")
    if not isinstance(dependency, str) or len(dependency) > 87384:
        anchor_service.fail(422, "invalid_base_value_source")
    try:
        raw = base64.b64decode(dependency, validate=True)
    except (ValueError, binascii.Error):
        anchor_service.fail(422, "invalid_base_value_source")
    if not raw or len(raw) > 65536 or base64.b64encode(raw).decode() != dependency:
        anchor_service.fail(422, "invalid_base_value_source")
    return hashlib.sha256(
        b"\x01" + share_id.bytes + entry_id.bytes + generation.bytes + raw
    ).hexdigest()


@dataclass(frozen=True)
class SourceObservation:
    identity_sha256: str
    observed_snapshot_sha256: str
    source_prefix_sha256: str
    source_complete: bool
    expires_at_ms: int
    deadline: float
    source_prefix: str = field(repr=False)

    @cached_property
    def _properties(self):
        # Private, immutable source observations may serve several properties
        # in one formula. Parse once per observation, never across snapshots or
        # accounts. Consumers still check the observation's binding and expiry.
        return metadata(self.source_prefix, complete=self.source_complete)

    def check(self, share_id, entry_id, generation, dependency):
        if self.identity_sha256 != source_identity(share_id, entry_id, generation, dependency):
            anchor_service.fail(409, "anchor_generation_mismatch")
        if anchor_service.now_ms() >= self.expires_at_ms or time.monotonic() >= self.deadline:
            anchor_service.fail(503, "anchor_validation_expired")


def observe(share_id: uuid.UUID, entry_id: uuid.UUID, generation: uuid.UUID, dependency: str):
    """Observe an existing authorized note through the authenticated private hop.

    Returns server-only data; never serialize this object into a public response.
    The observation proves a source snapshot, not user access or formula inputs.
    """
    if not enabled():
        anchor_service.fail(409, "base_value_sources_disabled")
    identity = source_identity(share_id, entry_id, generation, dependency)
    if not _slots.acquire(blocking=False):
        anchor_service.fail(503, "base_value_source_busy")
    try:
        deadline = time.monotonic() + 12
        request = {
            "protocol_version": 1,
            "request_id": str(uuid.uuid4()),
            "document_id": str(entry_id),
            "share_id": str(share_id),
            "generation": str(generation),
            "expires_at_ms": anchor_service.now_ms() + 15000,
        }
        origin = anchor_service.relay_origin()
        token = get_settings().collaboration_anchor_relay_token.get_secret_value()
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            anchor_service._post(client, origin, token, "register", request, deadline)
            request = {
                **request,
                "request_id": str(uuid.uuid4()),
                "source_dependency": dependency,
            }
            # JSON escaping can expand a bounded source prefix to six bytes per
            # character. The normal text-anchor response limit stays unchanged.
            response = anchor_service._post(
                client, origin, token, "source", request, deadline, response_limit=512 * 1024
            )
        prefix = response.get("source_prefix")
        snapshot, source_hash = (
            response.get("observed_snapshot_sha256"),
            response.get("source_prefix_sha256"),
        )
        if (
            not isinstance(prefix, str)
            or len(prefix) > MAX_PREFIX_BYTES
            or type(response.get("source_complete")) is not bool
            or not isinstance(snapshot, str)
            or not _hash.fullmatch(snapshot)
            or not isinstance(source_hash, str)
            or not _hash.fullmatch(source_hash)
        ):
            anchor_service.fail()
        try:
            encoded = prefix.encode("utf-8")
        except UnicodeError:
            anchor_service.fail()
        if len(encoded) > MAX_PREFIX_BYTES or hashlib.sha256(encoded).hexdigest() != source_hash:
            anchor_service.fail()
        result = SourceObservation(
            identity,
            snapshot,
            source_hash,
            response["source_complete"],
            request["expires_at_ms"],
            deadline,
            prefix,
        )
        result.check(share_id, entry_id, generation, dependency)
        return result
    finally:
        _slots.release()


class InvalidMetadata(ValueError):
    pass


class MetadataLoader(bases_definition.DefinitionLoader):
    """Use the bounded, non-executable YAML subset; never silently drop errors."""

    # Frontmatter uses YAML 1.2 core scalars. The .base definition parser has a
    # narrower scalar grammar; sharing its resolver would turn 0123/0o17/0x1f
    # into text even though the host exposes numeric groups.
    yaml_implicit_resolvers = {}

    def compose_node(self, parent, index):
        if self.depth >= 16 or self.nodes >= 4096:
            raise InvalidMetadata("Source metadata exceeds structural limits")
        return super().compose_node(parent, index)


_core_patterns = {
    "null": re.compile(r"^(?:~|null|Null|NULL|)$"),
    "bool": re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    "int": re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    "float": re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+"
        r"|[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)"
        r"|[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN)$"
    ),
}


def _core_scalar(loader, node):
    kind = node.tag.rsplit(":", 1)[-1]
    value = loader.construct_scalar(node)
    if not _core_patterns[kind].fullmatch(value):
        raise InvalidMetadata("Unsupported scalar encoding")
    if kind == "null":
        return None
    if kind == "bool":
        return value.lower() == "true"
    if kind == "int":
        return (
            int(value[2:], 8 if value[1] == "o" else 16)
            if value.startswith(("0o", "0x"))
            else int(value, 10)
        )
    if value.lower().endswith((".inf", ".nan")):
        raise InvalidMetadata("Unsupported property number")
    return float(value)


for _kind, _pattern in _core_patterns.items():
    _tag = "tag:yaml.org,2002:" + _kind
    MetadataLoader.add_implicit_resolver(_tag, _pattern, None)
    MetadataLoader.add_constructor(_tag, _core_scalar)


def _check_metadata(value):
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            _check_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _check_metadata(item)
    elif isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, float) and not math.isfinite(value):
        raise InvalidMetadata("Unsupported property number")
    elif isinstance(value, int) and abs(value) > 9007199254740991:
        raise InvalidMetadata("Property number exceeds exact shared precision")


def metadata(prefix: str, *, complete: bool) -> dict:
    """Extract the exact opening frontmatter block without inspecting body text.

    Fence behavior follows the installed host's getFrontMatterInfo: opening
    --- plus LF/CRLF, and a standalone closing --- at a line boundary. A prefix
    boundary is never interpreted as EOF. Parser parity for additional native
    YAML constructs remains required before minting the final group value proof.
    """
    try:
        if type(complete) is not bool or not isinstance(prefix, str):
            raise InvalidMetadata("Invalid source observation")
        if len(prefix.encode("utf-8")) > MAX_PREFIX_BYTES or prefix.startswith("\ufeff"):
            raise InvalidMetadata("Unsupported source observation")
        opening = re.match(r"^---\r?\n", prefix)
        if not opening:
            return {}
        closing = re.search(r"(?m)^---(?:\r?\n|$)", prefix[opening.end() :])
        if closing is None:
            if complete:
                return {}
            raise InvalidMetadata("Frontmatter exceeds observed prefix")
        end = opening.end() + closing.end()
        if end == len(prefix) and not complete and not prefix.endswith("\n"):
            raise InvalidMetadata("Frontmatter ends beyond observed prefix")
        value = yaml.load(
            prefix[opening.end() : opening.end() + closing.start()], Loader=MetadataLoader
        )
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise InvalidMetadata("Frontmatter is not a property mapping")
        _check_metadata(value)
        return value
    except (yaml.YAMLError, ValueError, RecursionError):
        # Do not include source snippets from YAML diagnostics in logs/errors.
        raise InvalidMetadata("Unsupported or malformed source metadata") from None


def property_value(observation: SourceObservation, name: str):
    if not isinstance(name, str) or not name or len(name) > 256 or any(ord(c) < 32 for c in name):
        raise InvalidMetadata("Invalid property name")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise InvalidMetadata("Invalid property name") from None
    values = observation._properties
    # Native getInsensitive prefers exact spelling, then the first lowercase
    # match in source order. Unicode casefold is stronger (Straße != STRASSE in
    # this API) and would incorrectly prove a different group's value.
    if name in values:
        return values[name]
    return next((value for key, value in values.items() if key.lower() == name.lower()), None)

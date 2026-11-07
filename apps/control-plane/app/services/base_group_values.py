"""Derive typed note-property values from private, verified source observations.

This does not authorize a user or create a durable group. The caller supplies a
server-side link resolver over current managed inventory, never a client-provided
map. Unreadable or ambiguous resolution must raise, not masquerade as a missing
link. Formula evaluation and file-property projection are separate adapters.
"""

import hashlib
import ipaddress
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services import base_source_service, bases_definition


class UnavailableValue(ValueError):
    """Retain the target/draft; never substitute a text or whole-view target."""


@dataclass(frozen=True)
class RecordIdentity:
    entry_id: uuid.UUID
    generation: uuid.UUID

    def wire(self):
        if not isinstance(self.entry_id, uuid.UUID) or not isinstance(self.generation, uuid.UUID):
            raise UnavailableValue("Invalid managed record identity")
        return {"record_id": str(self.entry_id), "record_generation": str(self.generation)}


@dataclass(frozen=True)
class DerivedValue:
    value: dict
    fingerprint: str
    references: frozenset[RecordIdentity]
    source_identity_sha256: str
    source_snapshot_sha256: str
    property_name: str
    time_zone: str


_date = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_instant = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?$"
)
_scheme = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _calendar(year, month, day):
    # JavaScript Date accepts a day up to 31 and rolls it into the following
    # month (2026-02-31 -> 2026-03-03). It does not roll month 13 or day 32.
    if not 1 <= year <= 9999 or not 1 <= month <= 12 or not 1 <= day <= 31:
        raise UnavailableValue("Unsupported calendar date")
    return datetime(year, month, 1) + timedelta(days=day - 1)


def _native_date(text, zone):
    calendar, instant = _date.fullmatch(text), _instant.fullmatch(text)
    if not calendar and not instant:
        return None
    try:
        matched = calendar or instant
        date = _calendar(*(int(matched[i]) for i in (1, 2, 3)))
        if calendar:
            # Round-trip through the zone: a skipped midnight or an entire
            # skipped day follows Date's forward disambiguation. Ordinary dates
            # remain dates; never convert them to an instant in the group key.
            local = date.replace(tzinfo=zone, fold=0)
            adjusted = local.astimezone(timezone.utc).astimezone(zone)
            return {"type": "date", "value": adjusted.strftime("%Y-%m-%d").zfill(10)}
        hour, minute, second = int(instant[4]), int(instant[5]), int(instant[6] or 0)
        milliseconds = int((instant[7] or "").ljust(3, "0")[:3])
        if (
            hour > 24
            or minute > 59
            or second > 59
            or hour == 24
            and (minute or second or milliseconds)
        ):
            raise UnavailableValue("Invalid clock time")
        date += timedelta(hours=hour, minutes=minute, seconds=second, milliseconds=milliseconds)
        offset = instant[8]
        if offset == "Z":
            date = date.replace(tzinfo=timezone.utc)
        elif offset:
            compact = offset[1:].replace(":", "")
            # The host's regex admits ±HH, but its Date parser yields Invalid
            # Date for that form. Preserve the resulting unavailable target.
            if len(compact) != 4 or int(compact[:2]) > 23 or int(compact[2:]) > 59:
                raise UnavailableValue("Invalid time offset")
            delta = timedelta(hours=int(compact[:2]), minutes=int(compact[2:]))
            date = date.replace(tzinfo=timezone(delta if offset[0] == "+" else -delta))
        else:
            # fold=0 chooses the earlier instant in a fold and the pre-gap
            # offset in a gap, matching Date's compatible disambiguation.
            date = date.replace(tzinfo=zone, fold=0)
        utc = date.astimezone(timezone.utc)
        return {
            "type": "instant",
            "value": utc.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
    except (OverflowError, ValueError):
        raise UnavailableValue("Unsupported date or instant") from None


def _native_url(text):
    """Prove a URL/text classification for an explicit bounded URL subset.

    Preserve the original spelling (the host does). Ambiguous WHATWG forms
    reject until their adapter has evidence; urllib is not a WHATWG replacement.
    """
    if " " in text:
        return False
    candidate = text.strip("\t\r\n")
    if not _scheme.match(candidate):
        return False
    if candidate != text or any(ord(c) < 32 or c == "\\" for c in text):
        raise UnavailableValue("Unsupported URL normalization")
    scheme = text.split(":", 1)[0].lower()
    special = scheme in {"http", "https", "ftp", "ws", "wss", "file"}
    authority = text[len(scheme) + 1 :].startswith("//")
    if not special and not authority:
        return True  # Absolute opaque URI; no host normalization is involved.
    if not authority:
        raise UnavailableValue("Unsupported special URL form")
    authority_text = re.split(r"[/?#]", text.split("//", 1)[1], maxsplit=1)[0]
    if any(ord(c) > 127 for c in authority_text):
        raise UnavailableValue("Unsupported URL authority normalization")
    if "@" in authority_text and any(c in authority_text.rsplit("@", 1)[0] for c in "[]"):
        raise UnavailableValue("Unsupported URL user information")
    try:
        parsed = urlsplit(text)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return False
    if scheme == "file" and (port is not None or parsed.username is not None):
        return False
    if not host:
        return (scheme == "file" or not special) and parsed.netloc == ""
    if ":" in host:
        if "%" in host:
            raise UnavailableValue("Unsupported scoped URL address")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise UnavailableValue("Unsupported URL host normalization")
    elif special and re.search(r"(?:^|\.)(?:[0-9]+|0x[^.]*)\.?$", host, re.IGNORECASE):
        # WHATWG's legacy IPv4 formats differ from urllib and may reject what
        # looks like a DNS name. Verify ordinary dotted decimal; defer the rest.
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise UnavailableValue("Unsupported URL address normalization") from None
    return True


def derive_note_value(
    observation: base_source_service.SourceObservation,
    property_name: str,
    source: RecordIdentity,
    *,
    share_id: uuid.UUID,
    dependency: str,
    source_path: str,
    time_zone: str,
    resolve_link: Callable[[str, str], RecordIdentity | None],
) -> DerivedValue:
    """Return a bounded typed value and all references actually used to derive it.

    Authorize the source/definition before invocation. The observation is checked
    here against the exact source lineage, share, dependency and expiry.
    The timezone describes the capturing host's interpretation of floating dates;
    it grants no authority. Persist it with the source evidence, not group identity.
    Native type changes, unresolved links and uncertainty never become plain text.
    """
    source.wire()
    observation.check(share_id, source.entry_id, source.generation, dependency)
    if not isinstance(time_zone, str) or not 0 < len(time_zone) <= 128:
        raise UnavailableValue("Invalid time zone")
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise UnavailableValue("Unavailable time zone") from None
    try:
        raw = base_source_service.property_value(observation, property_name)
    except base_source_service.InvalidMetadata:
        raise UnavailableValue("Unsupported source metadata") from None
    references = {source}
    nodes, characters = 256, 0

    def bounded_text(value):
        nonlocal characters
        if not isinstance(value, str):
            raise UnavailableValue("Invalid property text")
        try:
            characters += len(value.encode("utf-16-le")) // 2
        except UnicodeError:
            raise UnavailableValue("Invalid property Unicode") from None
        if characters > 8192:
            raise UnavailableValue("Group text exceeds the limit")
        return value

    def read(value, depth=0, tag=False):
        nonlocal nodes
        nodes -= 1
        if nodes < 0 or depth > 8:
            raise UnavailableValue("Group value exceeds structural limits")
        if tag:
            return {
                "type": "tag",
                "value": bounded_text(value if value.startswith("#") else "#" + value),
            }
        if value is None:
            return {"type": "empty"}
        if type(value) is bool:
            return {"type": "boolean", "value": value}
        if type(value) in {int, float}:
            number = float(value)
            if not math.isfinite(number):
                raise UnavailableValue("Unsupported property number")
            return {"type": "number", "value": 0.0 if number == 0 else number}
        if isinstance(value, list):
            if len(value) > nodes:
                raise UnavailableValue("Group list exceeds the limit")
            return {"type": "list", "value": [read(item, depth + 1) for item in value]}
        if not isinstance(value, str):
            raise UnavailableValue("Unsupported property type")
        if value.startswith("[[") and value.endswith("]]"):
            target = value[2:-2]
            if "|" in target:
                target = target.rsplit("|", 1)[0]
            bounded_text(target)
            path, separator, subpath = target.partition("#")
            bounded_text(source_path)
            destination = resolve_link(path, source_path)
            if destination is None:
                return {
                    "type": "unresolved_link",
                    "link": target,
                    **source.wire(),
                    "source_path_sha256": hashlib.sha256(source_path.encode()).hexdigest(),
                }
            if not isinstance(destination, RecordIdentity):
                raise UnavailableValue("Unverified link destination")
            references.add(destination)
            return {"type": "link", **destination.wire(), "subpath": separator + subpath}
        if _native_url(value):
            return {"type": "url", "value": bounded_text(value)}
        date = _native_date(value, zone)
        return date if date is not None else {"type": "text", "value": bounded_text(value)}

    is_tags = property_name.lower() == "tags" and (
        isinstance(raw, str) or isinstance(raw, list) and all(isinstance(v, str) for v in raw)
    )
    if is_tags:
        tags = [raw] if isinstance(raw, str) else raw
        if len(tags) >= nodes:
            raise UnavailableValue("Group list exceeds the limit")
        nodes -= 1
        value = {"type": "list", "value": [read(tag, 1, tag=True) for tag in tags]}
    else:
        value = read(raw)
    observation.check(share_id, source.entry_id, source.generation, dependency)
    return DerivedValue(
        value,
        bases_definition.fingerprint({"version": 1, "value": value}),
        frozenset(references),
        observation.identity_sha256,
        observation.observed_snapshot_sha256,
        property_name,
        time_zone,
    )

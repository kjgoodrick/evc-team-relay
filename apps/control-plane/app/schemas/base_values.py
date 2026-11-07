"""Bounded wire values. These validate identity syntax, never source authority."""

import math
import re
import uuid
from typing import Annotated

from pydantic import BeforeValidator


def normalize_group_value(value):
    nodes, characters = 256, 0

    def string(raw):
        nonlocal characters
        if not isinstance(raw, str):
            raise ValueError("Invalid group text")
        try:
            characters += len(raw.encode("utf-16-le")) // 2
        except UnicodeError:
            raise ValueError("Invalid group Unicode") from None
        if characters > 8192:
            raise ValueError("Group text exceeds limit")
        return raw

    def read(item, depth=0):
        nonlocal nodes
        nodes -= 1
        if nodes < 0 or depth > 8 or not isinstance(item, dict):
            raise ValueError("Invalid group structure")
        kind = item.get("type")
        fields = {
            "empty": set(),
            "text": {"value"},
            "number": {"value"},
            "boolean": {"value"},
            "date": {"value"},
            "instant": {"value"},
            "url": {"value"},
            "tag": {"value"},
            "list": {"value"},
            "duration": {"value"},
            "file": {"record_id", "record_generation"},
            "link": {"record_id", "record_generation", "subpath"},
            "unresolved_link": {"record_id", "record_generation", "link", "source_path_sha256"},
        }
        if not isinstance(kind, str) or kind not in fields or set(item) != fields[kind] | {"type"}:
            raise ValueError("Invalid group value fields")
        result = {"type": kind}
        if kind in {"file", "link", "unresolved_link"}:
            for key in ("record_id", "record_generation"):
                if not isinstance(item[key], str):
                    raise ValueError("Invalid group reference")
                result[key] = str(uuid.UUID(item[key]))
            if kind == "link":
                result["subpath"] = string(item["subpath"])
                if result["subpath"] and not result["subpath"].startswith("#"):
                    raise ValueError("Invalid link subpath")
            if kind == "unresolved_link":
                result["link"] = string(item["link"])
                digest = item["source_path_sha256"]
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Invalid source location")
                result["source_path_sha256"] = digest
        elif kind == "list":
            if not isinstance(item["value"], list) or len(item["value"]) > nodes:
                raise ValueError("Invalid group list")
            result["value"] = [read(child, depth + 1) for child in item["value"]]
        elif kind in {"number", "duration"}:
            raw = [item["value"]] if kind == "number" else item["value"]
            if not isinstance(raw, list) or len(raw) != (1 if kind == "number" else 7):
                raise ValueError("Invalid group number")
            values = []
            for number in raw:
                if type(number) not in {int, float} or not math.isfinite(number):
                    raise ValueError("Invalid group number")
                values.append(0.0 if number == 0 else float(number))
            result["value"] = values[0] if kind == "number" else values
        elif kind == "boolean":
            if type(item["value"]) is not bool:
                raise ValueError("Invalid group boolean")
            result["value"] = item["value"]
        elif kind != "empty":
            result["value"] = string(item["value"])
            if kind == "tag" and not result["value"].startswith("#"):
                raise ValueError("Invalid group tag")
            if kind == "date" and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", result["value"]):
                raise ValueError("Invalid group date")
            if kind == "instant" and not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", result["value"]
            ):
                raise ValueError("Invalid group instant")
        return result

    try:
        return read(value)
    except (OverflowError, RecursionError):
        raise ValueError("Group value exceeds limits") from None


GroupValue = Annotated[dict, BeforeValidator(normalize_group_value)]

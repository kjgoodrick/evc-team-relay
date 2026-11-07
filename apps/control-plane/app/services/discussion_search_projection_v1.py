"""Frozen v1 text projection shared by migration and command processing.

Change the projection format with a new version/backfill, not by rewriting this
module. Only visible text/alt text is indexed; URLs, IDs and history are absent.
"""

import re
import unicodedata
import uuid


def words(value: str) -> list[str]:
    return list(
        dict.fromkeys(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))
    )


def project(body: dict | None) -> tuple[str, str, set[uuid.UUID]]:
    mentions = set()

    def inline(nodes):
        parts = []
        for node in nodes:
            if node["type"] == "text":
                parts.append(node["text"])
            elif node["type"] == "link":
                parts.append(inline(node["children"]))
            elif node["type"] == "mention":
                mentions.add(uuid.UUID(node["user_id"]))
                parts.append(" ")
        return "".join(parts)

    lines = []
    for block in (body or {}).get("blocks", []):
        if block["type"] == "paragraph":
            lines.append(inline(block["children"]))
        elif block["type"] == "code":
            lines.append(block["text"])
        elif block["type"] == "list":
            lines.extend(inline(item["children"]) for item in block["items"])
        elif block["type"] == "picture":
            lines.append(block.get("alt", ""))
    text = "\n".join(lines)
    # These are actual words, not user-supplied tsquery syntax. Both PostgreSQL
    # and the lightweight SQLite fixture match the same normalized word set.
    terms = " ".join(word for word in words(text) if len(word) <= 256)
    return text, terms, mentions

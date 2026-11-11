"""Offline, read-only validation of portable discussion archives.

No database, credentials, network, HTML rendering, extraction or import. Hashes
prove internal consistency, not who created the archive or current authorization.
"""

import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from app.schemas import discussions as schema

MIB = 1024 * 1024
MAX_METADATA = 64 * MIB
MAX_PICTURES = 250 * MIB
MAX_SCOPES = 256
MAX_SECONDS = 60
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CHILD = re.compile("Discussions-" + UUID + "$", re.ASCII)
PICTURE = re.compile(r"pictures/[0-9]{5}\.(png|jpg)$", re.ASCII)
HASH = re.compile(r"[0-9a-f]{64}$", re.ASCII)
ANCHOR = TypeAdapter(schema.Anchor)


class ArchiveValidationError(ValueError):
    """Stable diagnostic without copied comments, paths or parser messages."""


def require(condition, code="invalid_archive"):
    if not condition:
        raise ArchiveValidationError(code)


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names.split()), "invalid_fields")


def identifier(value):
    return isinstance(value, str) and re.fullmatch(UUID, value, re.ASCII) is not None


def number(value, *, positive=False):
    return type(value) is int and (value > 0 if positive else value >= 0) and value <= 2**53 - 1


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def nullable_id(value):
    return value is None or identifier(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _finite(value, depth=0):
    require(depth <= 96, "json_nesting_limit")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _finite(item, depth + 1)
    elif isinstance(value, float):
        require(math.isfinite(value), "invalid_json_number")


def decode(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        _finite(value)
        require(isinstance(value, dict))
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ArchiveValidationError("invalid_json") from None


class Reader:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.metadata = 0
        self.pictures = 0
        self.files = 0

    def check(self):
        require(time.monotonic() <= self.deadline, "validation_time_limit")

    @contextmanager
    def directory(self, path, parent=None):
        self.check()
        require(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"), "unsupported_platform")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            raise ArchiveValidationError("directory_unavailable_or_symlink") from None
        try:
            yield fd
        finally:
            os.close(fd)

    def read(self, directory, name, limit, *, expected=None, binary=False):
        """Open only caller-validated single components beneath a held directory.

        Never trust an embedded path, follow a symlink, open a special file or
        read an unlisted extra. A changing file cannot satisfy verified length,
        hash and before/after stat checks silently.
        """
        self.check()
        require(
            isinstance(name, str)
            and name not in {"", ".", ".."}
            and "/" not in name
            and "\\" not in name
        )
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError:
            raise ArchiveValidationError("file_unavailable_or_symlink") from None
        try:
            before = os.fstat(fd)
            require(
                stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "non_regular_or_linked_file"
            )
            require(0 <= before.st_size <= limit, "file_size_limit")
            if expected is not None:
                require(
                    isinstance(expected, dict)
                    and number(expected.get("size"))
                    and isinstance(expected.get("sha256"), str)
                    and HASH.fullmatch(expected["sha256"]),
                    "invalid_file_manifest",
                )
                require(before.st_size == expected["size"], "file_length_mismatch")
            if binary:
                self.pictures += before.st_size
                require(self.pictures <= MAX_PICTURES, "picture_budget_exceeded")
            else:
                self.metadata += before.st_size
                require(self.metadata <= MAX_METADATA, "metadata_budget_exceeded")
            self.files += 1
            require(self.files <= 20000, "file_count_limit")
            digest, chunks, length = hashlib.sha256(), [], 0
            while True:
                self.check()
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                length += len(chunk)
                require(length <= before.st_size and length <= limit, "file_changed")
                digest.update(chunk)
                if not binary:
                    chunks.append(chunk)
            after = os.fstat(fd)
            require(
                length == before.st_size
                and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "file_changed",
            )
            if expected is not None:
                require(digest.hexdigest() == expected["sha256"], "file_hash_mismatch")
            return b"".join(chunks), {"size": length, "sha256": digest.hexdigest()}
        except OSError:
            raise ArchiveValidationError("file_read_failed") from None
        finally:
            os.close(fd)


def _seal(reader, directory, family, expected=None):
    raw, completion_hash = reader.read(directory, "COMPLETE.json", 4096, expected=expected)
    completion = decode(raw)
    key = "export_id" if family == "archive" else "catalog_id"
    fields(completion, "format version manifest manifestSha256 manifestBytes " + key)
    require(
        completion.get("format") == "team-relay-discussion-" + family + "-complete"
        and completion.get("version") == 1
        and type(completion.get("version")) is int,
        "unsupported_archive_format",
    )
    require(completion.get("manifest") == "manifest.json", "unsafe_manifest_path")
    raw, _ = reader.read(
        directory,
        "manifest.json",
        2 * MIB,
        expected={
            "size": completion.get("manifestBytes"),
            "sha256": completion.get("manifestSha256"),
        },
    )
    manifest = decode(raw)
    require(
        manifest.get("format") == "team-relay-discussion-" + family
        and manifest.get("version") == 1
        and type(manifest.get("version")) is int,
        "unsupported_archive_format",
    )
    fields(
        manifest,
        "format version share_id exporter_id picture_files files "
        + (
            "export_id captured_at entry_id view_id"
            if family == "archive"
            else "catalog_id inventory_captured_at completed_at atomic scopes"
        ),
    )
    require(
        identifier(manifest.get(key)) and manifest[key] == completion.get(key),
        "archive_identity_mismatch",
    )
    require(identifier(manifest.get("share_id")) and identifier(manifest.get("exporter_id")))
    return manifest, completion_hash


def _file_rows(manifest, primary):
    rows = manifest.get("files")
    require(isinstance(rows, list) and 2 <= len(rows) <= 5002, "invalid_file_manifest")
    files = {}
    for row in rows:
        require(
            isinstance(row, dict) and isinstance(row.get("file"), str) and row["file"] not in files,
            "duplicate_or_invalid_file",
        )
        name = row["file"]
        require(
            name in {primary, "README.txt"}
            or primary == "discussions.json"
            and PICTURE.fullmatch(name),
            "unsafe_archive_path",
        )
        fields(
            row,
            "file size sha256"
            + (" picture_id mime width height" if name.startswith("pictures/") else ""),
        )
        require(
            number(row.get("size"))
            and isinstance(row.get("sha256"), str)
            and HASH.fullmatch(row["sha256"]),
            "invalid_file_manifest",
        )
        files[name] = row
    require(primary in files and "README.txt" in files, "missing_archive_metadata")
    require(manifest.get("picture_files") in {"included", "not_selected"})
    return files


def _snapshot(value, manifest):
    fields(
        value,
        "format version export_id captured_at exporter_id share_id scope_id entry "
        "view_id collaboration_epoch sequence includes records",
    )
    require(
        value.get("format") == "team-relay-discussion-export"
        and type(value.get("version")) is int
        and value["version"] == 1,
        "unsupported_snapshot_format",
    )
    for field in ("export_id", "share_id", "exporter_id", "view_id"):
        require(value.get(field) == manifest.get(field), "archive_identity_mismatch")
    require(
        timestamp(value.get("captured_at")) and value["captured_at"] == manifest.get("captured_at")
    )
    entry = value.get("entry")
    fields(entry, "id path kind generation")
    require(
        isinstance(entry, dict)
        and identifier(entry.get("id"))
        and entry["id"] == manifest.get("entry_id")
        and entry.get("kind") in {"note", "image", "base"}
    )
    require(
        isinstance(entry.get("path"), str)
        and 0 < len(entry["path"]) <= 4096
        and nullable_id(entry.get("generation"))
    )
    require(
        nullable_id(value.get("scope_id"))
        and nullable_id(value.get("collaboration_epoch"))
        and number(value.get("sequence"))
    )
    require(
        identifier(value.get("view_id"))
        if entry["kind"] == "base"
        else value.get("view_id") is None
    )
    inclusion = {
        "message_history": True,
        "anchor_history": True,
        "picture_bytes": False,
        "removed_content": False,
    }
    fields(value.get("includes"), " ".join(inclusion))
    require(all(value["includes"][key] is expected for key, expected in inclusion.items()))
    rows = value.get("records")
    require(
        isinstance(rows, list) and len(rows) <= 5000 and (value["scope_id"] is not None or not rows)
    )
    threads, messages, anchors, revisions, pictures, reactions, identities = (
        {},
        {},
        {},
        {},
        {},
        {},
        set(),
    )
    people, required_pictures = set(), set()
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("type"), str))
        kind = row["type"]
        shape = {
            "thread": "id author_id state state_revision anchor_revision removed "
            "created_sequence activity_sequence created_at",
            "anchor_revision": "thread_id revision actor_id created_at anchor unavailable",
            "message": "id thread_id author_id revision deleted "
            "created_sequence created_at edited_at",
            "message_revision": "message_id revision body actor_id created_at",
            "reaction": "message_id emoji count mine",
            "picture": "picture_id available metadata",
            "identity": "id",
        }
        require(kind in shape, "unsupported_record_type")
        fields(row, "type " + shape[kind])
        for field in ("author_id", "actor_id"):
            if field in row:
                require(nullable_id(row[field]))
                if row[field]:
                    people.add(row[field])
        if kind == "thread":
            require(
                identifier(row.get("id"))
                and row["id"] not in threads
                and row.get("removed") is False
                and row.get("state") in {"open", "resolved"}
            )
            require(
                number(row.get("state_revision"), positive=True)
                and number(row.get("anchor_revision"), positive=True)
            )
            require(
                number(row.get("created_sequence"), positive=True)
                and number(row.get("activity_sequence"), positive=True)
                and row["created_sequence"] <= row["activity_sequence"] <= value["sequence"]
                and timestamp(row.get("created_at"))
            )
            threads[row["id"]] = row
        elif kind in {"anchor_revision", "message_revision"}:
            key_name = "thread_id" if kind == "anchor_revision" else "message_id"
            require(
                identifier(row.get(key_name))
                and number(row.get("revision"), positive=True)
                and timestamp(row.get("created_at"))
            )
            key = (row[key_name], row["revision"])
            destination = anchors if kind == "anchor_revision" else revisions
            require(key not in destination, "duplicate_revision")
            destination[key] = row
            if kind == "anchor_revision":
                require(
                    type(row.get("unavailable")) is bool
                    and row["unavailable"] == (row.get("anchor") is None)
                )
                if row["anchor"] is not None:
                    try:
                        anchor = ANCHOR.validate_python(row["anchor"])
                    except ValidationError:
                        raise ArchiveValidationError("invalid_anchor") from None
                    require(
                        (anchor.type == "base") == (entry["kind"] == "base")
                        and (anchor.type == "image_file") == (entry["kind"] == "image"),
                        "anchor_scope_mismatch",
                    )
                    if anchor.type == "base":
                        require(str(anchor.view_id) == value["view_id"], "anchor_scope_mismatch")
                    source = (
                        anchor
                        if anchor.type == "text"
                        else anchor.occurrence
                        if anchor.type == "image"
                        else None
                    )
                    require(
                        source is None or str(source.document_id) == entry["id"],
                        "anchor_scope_mismatch",
                    )
                    if anchor.type == "image_file":
                        require(str(anchor.asset.entry_id) == entry["id"], "anchor_scope_mismatch")
            elif row.get("body") is not None:
                try:
                    body = schema.MessageBody.model_validate(row["body"])
                except ValidationError:
                    raise ArchiveValidationError("invalid_message_body") from None
                required_pictures.update(str(pid) for pid in body.picture_ids())
                people.update(str(uid) for uid in body.mentioned_users())
        elif kind == "message":
            require(
                identifier(row.get("id"))
                and row["id"] not in messages
                and identifier(row.get("thread_id"))
                and number(row.get("revision"), positive=True)
                and type(row.get("deleted")) is bool
            )
            require(
                number(row.get("created_sequence"), positive=True)
                and row["created_sequence"] <= value["sequence"]
                and timestamp(row.get("created_at"))
                and (row.get("edited_at") is None or timestamp(row["edited_at"]))
            )
            messages[row["id"]] = row
        elif kind == "reaction":
            key = (row.get("message_id"), row.get("emoji"))
            require(
                identifier(key[0])
                and isinstance(key[1], str) and key[1] in schema.VALID_REACTIONS
                and key not in reactions
                and number(row.get("count"), positive=True)
                and type(row.get("mine")) is bool
            )
            reactions[key] = row
        elif kind == "picture":
            require(
                identifier(row.get("picture_id"))
                and row["picture_id"] not in pictures
                and type(row.get("available")) is bool
                and row["available"] == (row.get("metadata") is not None)
            )
            pictures[row["picture_id"]] = row
        elif kind == "identity":
            require(identifier(row.get("id")) and row["id"] not in identities)
            identities.add(row["id"])
        else:
            raise ArchiveValidationError("unsupported_record_type")
    require(
        people <= identities and required_pictures == set(pictures), "missing_archive_reference"
    )
    for thread in threads.values():
        require((thread["id"], thread["anchor_revision"]) in anchors, "missing_current_anchor")
    for thread_id, revision in anchors:
        require(
            thread_id in threads and revision <= threads[thread_id]["anchor_revision"],
            "orphan_anchor_revision",
        )
    for message in messages.values():
        require(message["thread_id"] in threads, "orphan_message")
        require(
            message["deleted"] or (message["id"], message["revision"]) in revisions,
            "missing_current_message",
        )
    for mid, revision in revisions:
        require(
            mid in messages
            and not messages[mid]["deleted"]
            and revision <= messages[mid]["revision"],
            "removed_or_orphan_body",
        )
    for mid, _emoji in reactions:
        require(mid in messages and not messages[mid]["deleted"], "removed_or_orphan_reaction")
    return pictures, {
        "threads": len(threads),
        "messages": len(messages),
        "message_revisions": len(revisions),
        "anchor_revisions": len(anchors),
        "picture_references": len(pictures),
    }


def _rendition(value, thumbnail=False):
    fields(value, "sha256 size mime width height")
    require(
        isinstance(value["sha256"], str)
        and HASH.fullmatch(value["sha256"])
        and number(value["size"], positive=True)
        and value["size"] <= (2 if thumbnail else 10) * MIB
        and value["mime"] in {"image/png", "image/jpeg"}
        and number(value["width"], positive=True)
        and number(value["height"], positive=True)
        and value["width"] * value["height"] <= 20_000_000
        and (not thumbnail or max(value["width"], value["height"]) <= 512),
        "invalid_picture_metadata",
    )


def _pictures(reader, directory, manifest, files, pictures):
    expected = {}
    for pid, row in pictures.items():
        metadata = row["metadata"]
        if not row["available"]:
            continue
        fields(
            metadata, "picture_id state source_sha256 source_size source_mime expires_at renditions"
        )
        require(
            metadata["picture_id"] == pid
            and metadata["state"] == "attached"
            and isinstance(metadata["source_sha256"], str)
            and HASH.fullmatch(metadata["source_sha256"])
            and number(metadata["source_size"], positive=True)
            and metadata["source_size"] <= 10 * MIB
            and metadata["source_mime"] in {"image/png", "image/jpeg", "image/webp", "image/gif"}
            and metadata["expires_at"] is None,
            "invalid_picture_metadata",
        )
        fields(metadata["renditions"], "content thumbnail")
        _rendition(metadata["renditions"]["content"])
        _rendition(metadata["renditions"]["thumbnail"], thumbnail=True)
        expected[pid] = metadata["renditions"]["content"]
    selected = [row for name, row in files.items() if name.startswith("pictures/")]
    if manifest["picture_files"] == "not_selected":
        require(not selected, "unexpected_picture_files")
        return 0
    require(len(expected) == len(pictures) == len(selected), "missing_picture_files")
    require(sum(row["size"] for row in selected) <= 100 * MIB, "scope_picture_budget_exceeded")
    if not selected:
        return 0
    seen = set()
    with reader.directory("pictures", directory) as picture_directory:
        for row in selected:
            pid = row["picture_id"]
            require(
                identifier(pid) and pid in expected and pid not in seen, "invalid_picture_reference"
            )
            seen.add(pid)
            rendition = expected[pid]
            require(
                number(row["width"], positive=True)
                and number(row["height"], positive=True)
                and all(row[field] == rendition[field] for field in rendition),
                "picture_metadata_mismatch",
            )
            suffix = ".png" if rendition["mime"] == "image/png" else ".jpg"
            require(row["file"].endswith(suffix), "picture_extension_mismatch")
            reader.read(
                picture_directory, row["file"].split("/")[1], 10 * MIB, expected=row, binary=True
            )
    return len(selected)


def _scope(reader, directory, expected=None):
    manifest, completion = _seal(reader, directory, "archive", expected)
    files = _file_rows(manifest, "discussions.json")
    raw, _ = reader.read(directory, "discussions.json", 8 * MIB, expected=files["discussions.json"])
    value = decode(raw)
    pictures, counts = _snapshot(value, manifest)
    reader.read(directory, "README.txt", 64 * 1024, expected=files["README.txt"])
    counts["picture_files"] = _pictures(reader, directory, manifest, files, pictures)
    return value, manifest, completion, counts


def _catalog(value, manifest):
    fields(
        value,
        "format version catalog_id captured_at exporter_id share_id "
        "collaboration_epoch sequence scopes",
    )
    require(
        value["format"] == "team-relay-discussion-export-catalog"
        and type(value["version"]) is int
        and value["version"] == 1,
        "unsupported_catalog_format",
    )
    require(
        all(value[key] == manifest[key] for key in ("catalog_id", "share_id", "exporter_id")),
        "archive_identity_mismatch",
    )
    require(
        timestamp(value["captured_at"])
        and value["captured_at"] == manifest["inventory_captured_at"]
        and timestamp(manifest["completed_at"])
        and manifest["atomic"] is False
    )
    require(nullable_id(value["collaboration_epoch"]) and number(value["sequence"]))
    scopes = value["scopes"]
    require(isinstance(scopes, list) and len(scopes) <= MAX_SCOPES, "scope_count_limit")
    previous, identities = "", {}
    for row in scopes:
        fields(row, "scope_id entry_id path kind view_id view_name view_state")
        require(
            identifier(row["scope_id"])
            and identifier(row["entry_id"])
            and isinstance(row["path"], str)
            and 0 < len(row["path"]) <= 4096
            and row["kind"] in {"note", "image", "base"}
        )
        require(
            identifier(row["view_id"])
            and isinstance(row["view_name"], str)
            and 0 < len(row["view_name"]) <= 512
            and row["view_state"] in {"active", "archived"}
            if row["kind"] == "base"
            else row["view_id"] is None and row["view_name"] is None and row["view_state"] is None
        )
        key = row["entry_id"] + ":" + (row["view_id"] or "")
        require(key > previous, "duplicate_or_unordered_scope")
        previous = key
        require(
            row["scope_id"] not in identities or identities[row["scope_id"]] == row["entry_id"],
            "archive_identity_mismatch",
        )
        identities[row["scope_id"]] = row["entry_id"]
    return scopes


def _collection(reader, directory):
    manifest, completion = _seal(reader, directory, "collection")
    files = _file_rows(manifest, "catalog.json")
    raw, _ = reader.read(directory, "catalog.json", 2 * MIB, expected=files["catalog.json"])
    value = decode(raw)
    scopes = _catalog(value, manifest)
    reader.read(directory, "README.txt", 64 * 1024, expected=files["README.txt"])
    children = manifest["scopes"]
    require(isinstance(children, list) and len(children) == len(scopes), "missing_scope_archives")
    seen, exports, counts = set(), set(), {}
    for scope, child in zip(scopes, children, strict=True):
        reader.check()
        fields(child, "entry_id view_id folder completionSha256 completionBytes")
        require(
            child["entry_id"] == scope["entry_id"] and child["view_id"] == scope["view_id"],
            "archive_identity_mismatch",
        )
        folder = child["folder"]
        require(
            isinstance(folder, str) and CHILD.fullmatch(folder) and folder not in seen,
            "unsafe_or_duplicate_scope_folder",
        )
        seen.add(folder)
        with reader.directory(folder, directory) as child_directory:
            snapshot, child_manifest, _, child_counts = _scope(
                reader,
                child_directory,
                {
                    "size": child["completionBytes"],
                    "sha256": child["completionSha256"],
                },
            )
        require(snapshot["export_id"] not in exports, "duplicate_export_identity")
        exports.add(snapshot["export_id"])
        require(
            snapshot["entry"]["id"] == scope["entry_id"]
            and snapshot["entry"]["kind"] == scope["kind"]
            and snapshot["view_id"] == scope["view_id"]
            and snapshot["scope_id"] == scope["scope_id"]
            and all(
                snapshot[key] == value[key]
                for key in ("share_id", "exporter_id", "collaboration_epoch")
            )
            and snapshot["sequence"] >= value["sequence"]
            and child_manifest["picture_files"] == manifest["picture_files"],
            "collection_scope_mismatch",
        )
        for key, count in child_counts.items():
            counts[key] = counts.get(key, 0) + count
    return value, manifest, completion, counts


def validate_archive(path):
    """Validate a selected directory without modifying it or opening any URL.

    Success only describes the bytes read. Archives are unsigned, may change
    after validation and do not establish current permissions or restore safety.
    Future import must revalidate its own captured input and perform live checks.
    """
    reader = Reader()
    try:
        with reader.directory(path) as directory:
            raw, _ = reader.read(directory, "COMPLETE.json", 4096)
            completion = decode(raw)
            family = completion.get("format")
            if family == "team-relay-discussion-archive-complete":
                value, manifest, seal, counts = _scope(reader, directory)
                scope_count, collection = 1, False
            elif family == "team-relay-discussion-collection-complete":
                value, manifest, seal, counts = _collection(reader, directory)
                scope_count, collection = len(value["scopes"]), True
            else:
                raise ArchiveValidationError("unsupported_archive_format")
        reader.check()
        return {
            "format": "team-relay-discussion-validation",
            "version": 1,
            "valid": True,
            "archive_type": "collection" if collection else "scope",
            "archive_id": value["catalog_id" if collection else "export_id"],
            "share_id": value["share_id"],
            "exporter_id": value["exporter_id"],
            "completion_sha256": seal["sha256"],
            "scopes": scope_count,
            "records": counts,
            "picture_files": manifest["picture_files"],
            "metadata_bytes_read": reader.metadata,
            "picture_bytes_read": reader.pictures,
            "warnings": [
                "unsigned_archive_origin_unverified",
                "current_access_not_checked",
                "not_a_coherent_service_backup",
                "no_import_performed",
                "picture_payloads_hashed_not_decoded",
                "unlisted_files_not_read",
            ],
        }
    except ArchiveValidationError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError):
        # Malformed JSON types must never expose text or crash a CLI consumer.
        raise ArchiveValidationError("invalid_archive") from None

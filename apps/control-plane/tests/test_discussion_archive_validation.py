"""Offline recovery validation: damaged/forged archives must fail closed."""

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.services import discussion_archive_validation as archive

STAMP = "2026-11-07T18:30:00.000Z"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def uid():
    return str(uuid.uuid4())


def snapshot(*, pictures=True):
    person, tid, mid, pid = uid(), uid(), uid(), uid()
    body = {
        "version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "children": [
                    {
                        "type": "text",
                        "text": "Never execute <script> or fetch https://example.invalid",
                    },
                    {"type": "mention", "user_id": person},
                ],
            }
        ],
    }
    if pictures:
        body["blocks"].append({"type": "picture", "picture_id": pid, "alt": "Example"})
    records = [
        {
            "type": "thread",
            "id": tid,
            "author_id": person,
            "state": "open",
            "state_revision": 1,
            "anchor_revision": 1,
            "removed": False,
            "created_sequence": 1,
            "activity_sequence": 3,
            "created_at": STAMP,
        },
        {
            "type": "anchor_revision",
            "thread_id": tid,
            "revision": 1,
            "actor_id": person,
            "created_at": STAMP,
            "anchor": {"version": 1, "type": "note"},
            "unavailable": False,
        },
        {
            "type": "message",
            "id": mid,
            "thread_id": tid,
            "author_id": person,
            "revision": 2,
            "deleted": False,
            "created_sequence": 1,
            "created_at": STAMP,
            "edited_at": STAMP,
        },
        {
            "type": "message_revision",
            "message_id": mid,
            "revision": 1,
            "body": body,
            "actor_id": person,
            "created_at": STAMP,
        },
        {
            "type": "message_revision",
            "message_id": mid,
            "revision": 2,
            "body": {"version": 1, "blocks": [{"type": "code", "text": "new text"}]},
            "actor_id": person,
            "created_at": STAMP,
        },
        {"type": "reaction", "message_id": mid, "emoji": "thumbs_up", "count": 2, "mine": True},
        {"type": "identity", "id": person},
    ]
    if pictures:
        rendition = {
            "sha256": hashlib.sha256(PNG).hexdigest(),
            "size": len(PNG),
            "mime": "image/png",
            "width": 1,
            "height": 1,
        }
        records.append(
            {
                "type": "picture",
                "picture_id": pid,
                "available": True,
                "metadata": {
                    "picture_id": pid,
                    "state": "attached",
                    "source_sha256": "a" * 64,
                    "source_size": 200,
                    "source_mime": "image/png",
                    "expires_at": None,
                    "renditions": {"content": rendition, "thumbnail": copy.deepcopy(rendition)},
                },
            }
        )
    return {
        "format": "team-relay-discussion-export",
        "version": 1,
        "export_id": uid(),
        "captured_at": STAMP,
        "exporter_id": person,
        "share_id": uid(),
        "scope_id": uid(),
        "entry": {"id": uid(), "path": "Team/Note.md", "kind": "note", "generation": uid()},
        "view_id": None,
        "collaboration_epoch": uid(),
        "sequence": 3,
        "includes": {
            "message_history": True,
            "anchor_history": True,
            "picture_bytes": False,
            "removed_content": False,
        },
        "records": records,
    }


def row(value, kind):
    return next(item for item in value["records"] if item["type"] == kind)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(raw)
    return {"file": path.name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def seal(path, manifest):
    metadata = write(path / "manifest.json", manifest)
    family = "collection" if "catalog_id" in manifest else "archive"
    key = "catalog_id" if family == "collection" else "export_id"
    return write(
        path / "COMPLETE.json",
        {
            "format": f"team-relay-discussion-{family}-complete",
            "version": 1,
            key: manifest[key],
            "manifest": "manifest.json",
            "manifestSha256": metadata["sha256"],
            "manifestBytes": metadata["size"],
        },
    )


def save_scope(path, value, *, include=True):
    files = [
        write(path / "discussions.json", value),
        write(path / "README.txt", b"Untrusted README"),
    ]
    if include:
        for index, picture in enumerate(
            r for r in value["records"] if r["type"] == "picture" and r["available"]
        ):
            filename = f"pictures/{index + 1:05}.png"
            write(path / filename, PNG)
            files.append(
                {
                    "file": filename,
                    "picture_id": picture["picture_id"],
                    **picture["metadata"]["renditions"]["content"],
                }
            )
    manifest = {
        "format": "team-relay-discussion-archive",
        "version": 1,
        **{
            key: value[key]
            for key in ("export_id", "captured_at", "exporter_id", "share_id", "view_id")
        },
        "entry_id": value["entry"]["id"],
        "picture_files": "included" if include else "not_selected",
        "files": files,
    }
    return seal(path, manifest)


def collection(path, *, empty=False):
    first, second = snapshot(), snapshot()
    for key in ("share_id", "exporter_id", "collaboration_epoch"):
        second[key] = first[key]
    values = [] if empty else sorted([first, second], key=lambda x: x["entry"]["id"])
    scopes, saved = [], []
    for value in values:
        scope = {
            "scope_id": value["scope_id"],
            "entry_id": value["entry"]["id"],
            "path": value["entry"]["path"],
            "kind": "note",
            "view_id": None,
            "view_name": None,
            "view_state": None,
        }
        scopes.append(scope)
        folder = "Discussions-" + uid()
        complete = save_scope(path / folder, value)
        saved.append(
            {
                "entry_id": scope["entry_id"],
                "view_id": None,
                "folder": folder,
                "completionSha256": complete["sha256"],
                "completionBytes": complete["size"],
            }
        )
    catalog = {
        "format": "team-relay-discussion-export-catalog",
        "version": 1,
        "catalog_id": uid(),
        "captured_at": STAMP,
        **{
            key: first[key]
            for key in ("share_id", "exporter_id", "collaboration_epoch", "sequence")
        },
        "scopes": scopes,
    }
    manifest = {
        "format": "team-relay-discussion-collection",
        "version": 1,
        **{key: catalog[key] for key in ("catalog_id", "share_id", "exporter_id")},
        "inventory_captured_at": STAMP,
        "completed_at": STAMP,
        "atomic": False,
        "picture_files": "included",
        "files": [write(path / "catalog.json", catalog), write(path / "README.txt", b"Collection")],
        "scopes": saved,
    }
    seal(path, manifest)
    return values, catalog, manifest


def test_complete_history_and_historical_picture_read_only(tmp_path, monkeypatch):
    value = snapshot()
    save_scope(tmp_path, value)
    (tmp_path / "unlisted-symlink").symlink_to("/etc/hosts")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.is_symlink()}
    import socket

    monkeypatch.setattr(
        socket, "socket", lambda *a, **kw: pytest.fail("offline validator opened a socket")
    )
    report = archive.validate_archive(tmp_path)
    assert report["valid"] and report["records"]["message_revisions"] == 2
    assert report["records"]["picture_files"] == 1 and report["picture_bytes_read"] == len(PNG)
    assert "unsigned_archive_origin_unverified" in report["warnings"]
    assert all(p.read_bytes() == data for p, data in before.items())
    assert "Never execute" not in json.dumps(report)


def test_metadata_only_allows_unavailable_picture_but_selected_does_not(tmp_path):
    value = snapshot()
    picture = row(value, "picture")
    picture.update(available=False, metadata=None)
    save_scope(tmp_path, value, include=False)
    assert archive.validate_archive(tmp_path)["records"]["picture_references"] == 1
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError, match="missing_picture_files"):
        archive.validate_archive(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: row(s, "message").update(deleted=True),
        lambda s: row(s, "message").update(body={"text": "removed body"}),
        lambda s: row(s, "message_revision").update(message_id=uid()),
        lambda s: row(s, "message_revision").update(revision=99),
        lambda s: s["records"].remove(row(s, "anchor_revision")),
        lambda s: s["records"].remove(row(s, "identity")),
        lambda s: s["records"].remove(row(s, "picture")),
        lambda s: s["records"].append(copy.deepcopy(row(s, "message"))),
        lambda s: row(s, "thread").update(removed=True),
        lambda s: row(s, "thread").update(activity_sequence=4),
        lambda s: row(s, "message").pop("author_id"),
        lambda s: s["includes"].update(removed_content=0),
        lambda s: s.update(scope_id=None),
        lambda s: row(s, "anchor_revision").update(
            anchor={"version": 1, "type": "image_file", "asset": {}}
        ),
        lambda s: row(s, "picture")["metadata"].update(object_key="private/data"),
        lambda s: row(s, "picture")["metadata"]["renditions"]["thumbnail"].update(width=513),
        lambda s: row(s, "message_revision")["body"]["blocks"][0]["children"].append(
            {
                "type": "link",
                "url": "javascript:alert(1)",
                "children": [{"type": "text", "text": "bad"}],
            }
        ),
        lambda s: s["records"].append({"type": "command.receipt"}),
        lambda s: s["entry"].update(kind=[]),
        lambda s: row(s, "reaction").update(emoji={}),
    ],
)
def test_recomputed_hashes_do_not_bypass_semantic_validation(tmp_path, mutation):
    value = snapshot()
    mutation(value)
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


def test_missing_latest_revision_and_tombstone_rules(tmp_path):
    value = snapshot(pictures=False)
    value["records"] = [
        r for r in value["records"] if r["type"] != "message_revision" or r["revision"] != 2
    ]
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError, match="missing_current_message"):
        archive.validate_archive(tmp_path)
    row(value, "message")["deleted"] = True
    value["records"] = [
        r for r in value["records"] if r["type"] not in {"message_revision", "reaction"}
    ]
    save_scope(tmp_path, value)
    assert archive.validate_archive(tmp_path)["records"]["message_revisions"] == 0


@pytest.mark.parametrize(
    "name",
    ["discussions.json", "README.txt", "pictures/00001.png", "manifest.json", "COMPLETE.json"],
)
def test_corruption_truncation_and_missing_file(tmp_path, name):
    value = snapshot()
    save_scope(tmp_path, value)
    target = tmp_path / name
    original = target.read_bytes()
    target.write_bytes(b"X" + original[1:])
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)
    target.write_bytes(original[:-1])
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)
    target.unlink()
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_never_reads_linked_or_special_files(tmp_path, kind):
    root, outside = tmp_path / "archive", tmp_path / "outside"
    save_scope(root, snapshot())
    target = root / "discussions.json"
    target.rename(outside)
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(root)
    assert outside.is_file()


def test_picture_and_root_symlink_directories_rejected(tmp_path):
    root = tmp_path / "archive"
    save_scope(root, snapshot())
    (root / "pictures").rename(tmp_path / "elsewhere")
    (root / "pictures").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(archive.ArchiveValidationError, match="directory_unavailable_or_symlink"):
        archive.validate_archive(root)
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    with pytest.raises(archive.ArchiveValidationError, match="directory_unavailable_or_symlink"):
        archive.validate_archive(tmp_path / "alias")


@pytest.mark.parametrize(
    "bad",
    ["../escape", "/tmp/escape", "pictures/../escape", "pictures/00001.svg", "pictures\\00001.png"],
)
def test_manifest_paths_never_escape_archive(tmp_path, bad):
    save_scope(tmp_path, snapshot())
    manifest = json.loads((tmp_path / "manifest.json").read_bytes())
    manifest["files"][2]["file"] = bad
    seal(tmp_path, manifest)
    with pytest.raises(archive.ArchiveValidationError, match="unsafe_archive_path"):
        archive.validate_archive(tmp_path)


@pytest.mark.parametrize("empty", [False, True])
def test_collection_binds_inventory_and_child_completions(tmp_path, empty):
    collection(tmp_path, empty=empty)
    result = archive.validate_archive(tmp_path)
    assert result["archive_type"] == "collection"
    assert result["scopes"] == (0 if empty else 2)
    assert result["records"].get("message_revisions", 0) == (0 if empty else 4)


@pytest.mark.parametrize(
    "field",
    ["share_id", "exporter_id", "collaboration_epoch", "scope_id", "sequence", "entry", "view_id"],
)
def test_resealed_collection_cannot_mix_child_scope_or_history(tmp_path, field):
    values, catalog, manifest = collection(tmp_path)
    value = values[0]
    if field == "sequence":
        catalog["sequence"] = 4
        manifest["files"][0] = write(tmp_path / "catalog.json", catalog)
    elif field == "entry":
        value["entry"]["id"] = uid()
    else:
        value[field] = uid()
    child = manifest["scopes"][0]
    complete = save_scope(tmp_path / child["folder"], value)
    child.update(completionSha256=complete["sha256"], completionBytes=complete["size"])
    seal(tmp_path, manifest)
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


def test_incomplete_collection_keeps_complete_children_independently_valid(tmp_path):
    _, _, manifest = collection(tmp_path)
    (tmp_path / "COMPLETE.json").unlink()
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)
    for child in manifest["scopes"]:
        assert archive.validate_archive(tmp_path / child["folder"])["valid"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["scopes"].pop(),
        lambda m: m["scopes"][0].update(folder="../outside"),
        lambda m: m["scopes"][0].update(completionSha256="0" * 64),
        lambda m: m["scopes"][1].update(folder=m["scopes"][0]["folder"]),
        lambda m: m.update(atomic=True),
        lambda m: m.update(picture_files="not_selected"),
    ],
)
def test_collection_manifest_requires_all_original_children(tmp_path, mutation):
    _, _, manifest = collection(tmp_path)
    mutation(manifest)
    seal(tmp_path, manifest)
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"n":NaN}',
        b'{"n":1e9999}',
        b'{"s":"\xff"}',
        b'{"x":' + b"[" * 100 + b"0" + b"]" * 100 + b"}",
    ],
)
def test_ambiguous_or_excessively_nested_json_rejected(raw):
    with pytest.raises(archive.ArchiveValidationError):
        archive.decode(raw)


@pytest.mark.parametrize("budget", ["MAX_METADATA", "MAX_PICTURES", "MAX_SECONDS", "MAX_SCOPES"])
def test_resource_budgets_fail_without_partial_success(tmp_path, monkeypatch, budget):
    collection(tmp_path)
    monkeypatch.setattr(archive, budget, 0)
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


def test_cli_has_no_application_database_or_network_startup(tmp_path):
    save_scope(tmp_path, snapshot())
    root = Path(__file__).resolve().parents[3]
    code = """
import importlib.abc, runpy, sys
class NoServer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {"app.main", "app.core.config", "sqlalchemy", "socket"}:
            raise AssertionError("unexpected offline dependency: " + fullname)
sys.meta_path.insert(0, NoServer())
sys.argv = [sys.argv[1], sys.argv[2]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    command = [
        sys.executable,
        "-c",
        code,
        str(root / "scripts/validate-discussion-archive.py"),
        str(tmp_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert json.loads(result.stdout)["valid"]
    (tmp_path / "COMPLETE.json").unlink()
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout) == {"valid": False, "error": "file_unavailable_or_symlink"}


def test_lazy_app_export_remains_asgi_compatible():
    from app import app
    from app.main import app as main_app

    assert app is main_app


def test_saved_view_anchor_stays_in_its_view_and_redaction_is_explicit(tmp_path):
    value = snapshot(pictures=False)
    value["entry"].update(kind="base", path="Team/Planning.base", generation=None)
    value["view_id"] = uid()
    anchor = row(value, "anchor_revision")
    anchor["anchor"] = {
        "version": 1,
        "type": "base",
        "definition_id": uid(),
        "view_id": value["view_id"],
        "generation": uid(),
        "registry_revision": 1,
        "target": {"type": "view"},
    }
    save_scope(tmp_path, value)
    assert archive.validate_archive(tmp_path)["valid"]
    anchor["anchor"]["view_id"] = uid()
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError, match="anchor_scope_mismatch"):
        archive.validate_archive(tmp_path)
    anchor.update(anchor=None, unavailable=True)
    save_scope(tmp_path, value)
    assert archive.validate_archive(tmp_path)["valid"]
    anchor["unavailable"] = False
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError):
        archive.validate_archive(tmp_path)


def test_picture_hashes_must_match_captured_metadata_even_if_manifest_resealed(tmp_path):
    save_scope(tmp_path, snapshot())
    manifest = json.loads((tmp_path / "manifest.json").read_bytes())
    picture = manifest["files"][2]
    picture["sha256"] = write(tmp_path / picture["file"], b"replacement")["sha256"]
    picture["size"] = len(b"replacement")
    seal(tmp_path, manifest)
    with pytest.raises(archive.ArchiveValidationError, match="picture_metadata_mismatch"):
        archive.validate_archive(tmp_path)


def test_scope_picture_budget_is_independent_of_collection_limit(tmp_path, monkeypatch):
    # Exercise the lower per-scope cap without creating or reading large files.
    value = snapshot()
    picture = row(value, "picture")
    picture["metadata"]["renditions"]["content"]["size"] = 10 * archive.MIB
    body = row(value, "message_revision")["body"]
    for index in range(10):
        extra = copy.deepcopy(picture)
        extra["picture_id"] = extra["metadata"]["picture_id"] = uid()
        value["records"].append(extra)
        value["records"].append(
            {
                "type": "message_revision",
                "message_id": row(value, "message")["id"],
                "revision": index + 3,
                "body": {
                    "version": 1,
                    "blocks": [{"type": "picture", "picture_id": extra["picture_id"]}],
                },
                "actor_id": None,
                "created_at": STAMP,
            }
        )
    row(value, "message")["revision"] = 12
    save_scope(tmp_path, value)
    with pytest.raises(archive.ArchiveValidationError, match="scope_picture_budget_exceeded"):
        archive.validate_archive(tmp_path)

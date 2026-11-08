#!/usr/bin/env python3
"""Capture a cold snapshot of an explicitly labelled, isolated Relay stack.

This stops services. It never starts, force-kills, removes or recreates them.
See docs/collaboration-snapshots.md for the supported storage contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tarfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SET_LABEL = "io.evc.snapshot.set"
ROLE_LABEL = "io.evc.snapshot.role"
FORMAT = "evc-cold-snapshot-v2"
LEGACY_FORMAT = "evc-cold-snapshot-v1"
SUPPORTED_FORMATS = {FORMAT, LEGACY_FORMAT}
CONTRACT = "single-postgres-minio-local-relay-v1"
PATHS = {
    "postgres": "/var/lib/postgresql/data",
    "objects": "/data",
    "relay": "/app/data",
    "control-plane": "/app/app/static/uploads",
}
ORDER = {
    "ingress": 0,
    "worker": 1,
    "control-plane": 2,
    "relay": 3,
    "postgres": 4,
    "objects": 5,
}
HEX_ID = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
COMPLETION = "All document writers stopped and final checkpoints completed"
MAX_JSON = 4 * 1024 * 1024


class SnapshotError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise SnapshotError(message)


def now():
    return datetime.now(UTC).isoformat()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def timestamp(value):
    require(isinstance(value, str), "Invalid snapshot timestamp")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None and parsed.year >= 1970, "Invalid snapshot timestamp")
    return parsed


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def private_json(path, value):
    """Exclusive, durable write; never overwrite evidence from an earlier run."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path):
    require(path.is_file() and not path.is_symlink(), "Expected a regular JSON file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    require(len(raw) <= MAX_JSON, "JSON exceeds the snapshot limit")

    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise SnapshotError("Nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_id(value):
    require(
        isinstance(value, str) and HEX_ID.fullmatch(value),
        "Expected a full container ID",
    )
    return value


def validate_plan(plan):
    require(
        isinstance(plan, dict)
        and set(plan) == {"format", "storage_contract", "set_id", "source_commit", "containers"},
        "Unsupported snapshot plan",
    )
    require(
        plan["format"] in SUPPORTED_FORMATS and plan["storage_contract"] == CONTRACT,
        "Unsupported storage contract",
    )
    require(str(uuid.UUID(plan["set_id"])) == plan["set_id"], "Invalid snapshot set ID")
    require(
        isinstance(plan["source_commit"], str) and COMMIT.fullmatch(plan["source_commit"]),
        "Expected an explicitly selected source commit",
    )
    rows = plan["containers"]
    require(isinstance(rows, list) and 4 <= len(rows) <= 32, "Invalid container set")
    ids = set()
    roles = []
    for row in rows:
        require(
            isinstance(row, dict) and set(row) == {"id", "role", "image", "configuration"},
            "Invalid container identity",
        )
        canonical_id(row["id"])
        require(
            row["id"] not in ids and row["role"] in ORDER,
            "Duplicate ID or unsupported role",
        )
        require(
            isinstance(row["image"], str)
            and HEX_ID.fullmatch(row["image"].removeprefix("sha256:")),
            "Expected an immutable image ID",
        )
        require(
            isinstance(row["configuration"], str) and HEX_ID.fullmatch(row["configuration"]),
            "Invalid configuration fingerprint",
        )
        ids.add(row["id"])
        roles.append(row["role"])
    require(
        all(roles.count(role) == 1 for role in PATHS),
        "Each storage role must occur exactly once",
    )
    return plan


def configuration(info):
    # Environment and command values can contain secrets. Only the digest leaves
    # memory; do not include raw inspect output in an archive or an error.
    return digest({key: info.get(key) for key in ("Config", "HostConfig", "Mounts")})


class Engine:
    def __init__(self, binary="docker", timeout=300):
        require(binary in ("docker", "podman"), "Unsupported container engine")
        self.binary = binary
        self.timeout = timeout

    def command(self, args, output=None, input_file=None):
        try:
            result = subprocess.run(
                [self.binary, *args],
                stdout=output or subprocess.PIPE,
                stdin=input_file,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Killing the CLI on timeout does not force-kill the container. The
            # daemon may still be draining it. No archive is sealed or resumed.
            raise SnapshotError("Container engine operation failed or timed out") from exc
        require(result.returncode == 0, "Container engine operation failed")
        return result.stdout

    def ids(self, set_id=None):
        args = ["ps", "-aq", "--no-trunc"]
        if set_id:
            args += ["--filter", f"label={SET_LABEL}={set_id}"]
        return [canonical_id(value) for value in self.command(args).decode().split()]

    def inspect(self, container_id):
        data = json.loads(self.command(["inspect", canonical_id(container_id)]))
        require(isinstance(data, list) and len(data) == 1, "Invalid container inspection")
        require(data[0]["Id"] == container_id, "Container identity changed")
        return data[0]

    def stop(self, container_id):
        # Both engines document -1 as infinite graceful wait. The local command
        # deadline aborts this capture without asking the engine to force-kill.
        self.command(["stop", "--time", "-1", canonical_id(container_id)])

    def logs(self, container_id, started_at):
        try:
            result = subprocess.run(
                [
                    self.binary,
                    "logs",
                    "--since",
                    started_at,
                    "--tail",
                    "100",
                    container_id,
                ],
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotError("Cannot verify relay completion") from exc
        require(result.returncode == 0, "Cannot verify relay completion")
        return result.stdout + result.stderr

    def archive(self, container_id, path, destination):
        with os.fdopen(
            os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as stream:
            self.command(["cp", f"{canonical_id(container_id)}:{path}/.", "-"], output=stream)
            stream.flush()
            os.fsync(stream.fileno())

    def restore(self, container_id, path, source):
        with source.open("rb") as stream:
            self.command(
                ["cp", "--archive", "-", f"{canonical_id(container_id)}:{path}"],
                input_file=stream,
            )


def live(info):
    state = info["State"]
    return state.get("Running") is True and not any(
        state.get(k) for k in ("Paused", "Restarting", "Dead")
    )


def normal_exit(role, code):
    # Uvicorn re-raises SIGTERM/SIGINT after a completed lifespan shutdown.
    # These statuses require its completion evidence separately at capture.
    return type(code) is int and (code == 0 or role == "control-plane" and code in (130, 143))


def mounts(info):
    return [m for m in info.get("Mounts", []) if m.get("Type") in ("bind", "volume")]


def mount_identity(mount):
    # A named volume's Source is a stable engine-side path on both engines.
    require(
        isinstance(mount.get("Source"), str) and mount["Source"].startswith("/"),
        "Cannot establish storage mount identity",
    )
    return PurePosixPath(mount["Source"])


def validate_layout(infos, rows, others):
    captured = []
    for row in rows:
        info = infos[row["id"]]
        role = row["role"]
        writable = [m for m in mounts(info) if m.get("RW") is True]
        if role in PATHS:
            require(
                len(writable) == 1 and writable[0]["Destination"] == PATHS[role],
                "Storage role must have exactly its supported writable mount",
            )
            captured.append(mount_identity(writable[0]))
        else:
            require(not writable, "Additional persistent writer storage is unsupported")
    for index, source in enumerate(captured):
        require(
            not any(
                source == other or source in other.parents or other in source.parents
                for other in captured[:index]
            ),
            "Snapshot stores overlap",
        )
        for other in others:
            for mount in mounts(other):
                target = mount_identity(mount)
                require(
                    not (source == target or source in target.parents or target in source.parents),
                    "A container outside this snapshot set shares its storage",
                )


def inspect_set(engine, set_id):
    expected = engine.ids(set_id)
    require(4 <= len(expected) <= 32, "Invalid labelled container set")
    all_ids = engine.ids()
    require(set(expected) <= set(all_ids), "Container set changed during inspection")
    return (
        {cid: engine.inspect(cid) for cid in expected},
        [engine.inspect(cid) for cid in all_ids if cid not in expected],
    )


def fresh_isolated(info):
    state = info["State"]
    return (
        state.get("Status") in ("created", "configured")
        and not state.get("Running")
        and not state.get("Paused")
        and not info.get("RestartCount", 0)
        and info.get("HostConfig", {}).get("NetworkMode") == "none"
    )


def prepare(engine, set_id, source_commit, fresh=False):
    require(str(uuid.UUID(set_id)) == set_id, "Invalid snapshot set ID")
    infos, others = inspect_set(engine, set_id)
    rows = []
    for cid, info in infos.items():
        require(
            fresh_isolated(info) if fresh else live(info),
            "Expected new network-isolated containers"
            if fresh
            else "All selected containers must be running before capture",
        )
        labels = info["Config"].get("Labels") or {}
        require(labels.get(SET_LABEL) == set_id, "Snapshot label changed")
        rows.append(
            {
                "id": cid,
                "role": labels.get(ROLE_LABEL),
                "image": info["Image"],
                "configuration": configuration(info),
            }
        )
    plan = validate_plan(
        {
            "format": FORMAT,
            "storage_contract": CONTRACT,
            "set_id": set_id,
            "source_commit": source_commit,
            "containers": sorted(rows, key=lambda r: r["id"]),
        }
    )
    validate_layout(infos, rows, others)
    return plan


def check_set(engine, plan, initial, stopped):
    infos, others = inspect_set(engine, plan["set_id"])
    require(set(infos) == set(initial), "Snapshot container membership changed")
    for row in plan["containers"]:
        cid = row["id"]
        info = infos[cid]
        require(
            info["Image"] == row["image"] and configuration(info) == row["configuration"],
            "Container configuration changed",
        )
        state, previous = info["State"], initial[cid]["State"]
        require(
            state.get("StartedAt") == previous.get("StartedAt")
            and info.get("RestartCount", 0) == initial[cid].get("RestartCount", 0),
            "Container restarted during snapshot",
        )
        if cid in stopped:
            require(
                state.get("Status") in ("exited", "stopped")
                and not state.get("Running")
                and normal_exit(row["role"], state.get("ExitCode"))
                and not state.get("OOMKilled")
                and not state.get("Error")
                and state.get("FinishedAt"),
                "Container did not stop cleanly",
            )
            require(state["FinishedAt"] == stopped[cid], "Container stop evidence changed")
            require(
                timestamp(state["FinishedAt"]) >= timestamp(state["StartedAt"]),
                "Invalid shutdown timestamps",
            )
        else:
            require(
                live(info),
                "Container stopped before the coordinator admitted its shutdown",
            )
    validate_layout(infos, plan["containers"], others)
    return infos


def archive_info(path):
    require(path.is_file() and not path.is_symlink(), "Expected a regular store archive")
    before = path.stat()
    total = 0
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            total += len(chunk)
    require(total % 512 == 0 and total >= 1024, "Invalid tar length")
    seen = set()
    files = set()
    parents = set()
    contents = 0
    tree = []
    empty = True
    with tarfile.open(path, mode="r:") as archive:
        for entry in archive:
            name = entry.name
            require(
                0 < len(name) <= 4096 and "\\" not in name and "\x00" not in name,
                "Unsupported archive path",
            )
            parts = name.split("/")
            require(not name.startswith("/") and ".." not in parts, "Unsafe archive path")
            normalized = str(PurePosixPath(name))
            require(normalized not in seen, "Duplicate archive path")
            require(
                entry.isdir() or entry.isreg(),
                "Links and special files are unsupported",
            )
            require(
                entry.sparse is None and not any("sparse" in k.lower() for k in entry.pax_headers),
                "Sparse archive entries are unsupported",
            )
            require(
                entry.size >= 0 and (entry.isreg() or entry.size == 0),
                "Invalid archive entry",
            )
            require(normalized != "." or entry.isdir(), "Invalid archive root")
            require(
                entry.isdir() or normalized not in parents,
                "File replaces an archive directory",
            )
            for parent in PurePosixPath(normalized).parents:
                require(str(parent) not in files, "Archive path descends from a file")
                parents.add(str(parent))
            seen.add(normalized)
            body_hash = hashlib.sha256()
            if entry.isreg():
                files.add(normalized)
                with archive.extractfile(entry) as body:
                    while chunk := body.read(1024 * 1024):
                        body_hash.update(chunk)
            require(
                0 <= entry.uid <= 2**32 - 1
                and 0 <= entry.gid <= 2**32 - 1
                and 0 <= entry.mode <= 0o7777,
                "Unsupported archive ownership or mode",
            )
            tree.append(
                [
                    normalized,
                    "directory" if entry.isdir() else "file",
                    entry.mode,
                    entry.uid,
                    entry.gid,
                    entry.size,
                    body_hash.hexdigest() if entry.isreg() else None,
                ]
            )
            empty = empty and normalized == "." and entry.isdir()
            contents += entry.size
            require(
                len(seen) <= 1_000_000 and contents <= total,
                "Archive exceeds structural limits",
            )
        end = archive.offset
    # tar readers stop at the first terminator; reject a hidden trailing archive.
    with path.open("rb") as stream:
        stream.seek(end)
        trailer = 0
        while chunk := stream.read(1024 * 1024):
            require(not any(chunk), "Nonzero data follows the tar terminator")
            trailer += len(chunk)
    require(trailer >= 1024, "Missing tar terminator")
    after = path.stat()
    require(
        (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ),
        "Archive changed during verification",
    )
    return {
        "sha256": hasher.hexdigest(),
        "bytes": total,
        "entries": len(seen),
        "content_bytes": contents,
        "tree_sha256": digest(sorted(tree)),
        "empty": empty,
    }


def stage(engine, folder, target_plan, output):
    """Populate only fresh, isolated, empty storage. Never activate a restore."""
    source = verify(folder)
    validate_plan(target_plan)
    require(
        target_plan["set_id"] != source["plan"]["set_id"],
        "Restore requires a new container set",
    )
    require(
        not (
            {row["id"] for row in target_plan["containers"]}
            & {row["id"] for row in source["plan"]["containers"]}
        ),
        "Restore cannot reuse source containers",
    )
    require(
        target_plan["source_commit"] == source["plan"]["source_commit"],
        "Restore source revision does not match the captured revision",
    )

    def images(plan):
        return sorted((row["role"], row["image"]) for row in plan["containers"])

    require(
        images(target_plan) == images(source["plan"]),
        "Restore requires the captured image identities for every role",
    )

    def check_target():
        require(
            prepare(engine, target_plan["set_id"], target_plan["source_commit"], fresh=True)
            == target_plan,
            "Restore target changed or was started",
        )

    check_target()
    output.mkdir(mode=0o700)
    private_json(output / "source-manifest.json", source)
    private_json(output / "target-plan.json", target_plan)
    rows = [row for row in target_plan["containers"] if row["role"] in PATHS]
    # Prove every destination empty before the first write, not midway through
    # a restore. Keep probes and readback evidence in this private work folder.
    for row in rows:
        check_target()
        probe = output / f"empty-{row['role']}.tar"
        engine.archive(row["id"], PATHS[row["role"]], probe)
        require(archive_info(probe)["empty"], "Restore destination is not empty")
        check_target()
    started = time.monotonic()
    for row in rows:
        role = row["role"]
        original = folder / f"{role}.tar"
        require(
            archive_info(original) == source["archives"][role],
            "Source archive changed before restore",
        )
        check_target()
        engine.restore(row["id"], PATHS[role], original)
        check_target()
        readback = output / f"restored-{role}.tar"
        engine.archive(row["id"], PATHS[role], readback)
        restored = archive_info(readback)
        require(
            restored["tree_sha256"] == source["archives"][role]["tree_sha256"],
            "Restored file content, ownership or modes do not match",
        )
        private_json(output / f"verified-{role}.json", restored)
        check_target()
    require(verify(folder) == source, "Source snapshot changed during restore")
    check_target()
    receipt = {
        "format": FORMAT,
        "source_manifest_sha256": digest(source),
        "target_plan_sha256": digest(target_plan),
        "completed_at": now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "state": "staged-offline-requires-generation-rotation-and-rehearsal",
    }
    private_json(output / "staged.json", receipt)
    sync_directory(output)
    sync_directory(output.parent)
    return receipt


def capture(engine, plan, output):
    validate_plan(plan)
    require(
        prepare(engine, plan["set_id"], plan["source_commit"]) == plan,
        "Prepared snapshot plan is stale",
    )
    output.mkdir(mode=0o700)
    private_json(output / "plan.json", plan)
    initial, _ = inspect_set(engine, plan["set_id"])
    stopped = {}
    stop_evidence = {}
    began = now()
    started = time.monotonic()
    for number, row in enumerate(sorted(plan["containers"], key=lambda r: ORDER[r["role"]])):
        check_set(engine, plan, initial, stopped)
        cid = row["id"]
        engine.stop(cid)
        state = engine.inspect(cid)["State"]
        stopped[cid] = state.get("FinishedAt")
        check_set(engine, plan, initial, stopped)
        if row["role"] == "relay":
            log = engine.logs(cid, initial[cid]["State"]["StartedAt"])
            require(
                COMPLETION.encode() in log and b"Server shut down." in log,
                "Relay final-checkpoint completion is missing",
            )
        if row["role"] == "control-plane":
            log = engine.logs(cid, initial[cid]["State"]["StartedAt"])
            require(
                b"control_plane_shutdown_complete" in log
                and b"Application shutdown complete." in log
                and b"Finished server process [" in log
                and b"Application shutdown failed." not in log,
                "Control-plane lifespan shutdown completion is missing",
            )
            require(
                b"ASGI 'lifespan' protocol appears unsupported." not in log
                and b"Exception in 'lifespan' protocol" not in log,
                "Control-plane lifespan shutdown completion is missing",
            )
        stop_evidence[cid] = {
            "id": cid,
            "role": row["role"],
            "started_at": initial[cid]["State"]["StartedAt"],
            "finished_at": stopped[cid],
            "exit_code": state["ExitCode"],
        }
        private_json(
            output / f"stop-{number:02d}.json",
            stop_evidence[cid],
        )
    quiesced_at = now()
    archives = {}
    for row in plan["containers"]:
        if row["role"] not in PATHS:
            continue
        check_set(engine, plan, initial, stopped)
        role = row["role"]
        destination = output / f"{role}.tar"
        engine.archive(row["id"], PATHS[role], destination)
        archives[role] = archive_info(destination)
        check_set(engine, plan, initial, stopped)
    manifest = {
        "format": FORMAT,
        "plan": plan,
        "started_at": began,
        "quiesced_at": quiesced_at,
        "completed_at": now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "archives": archives,
        "stops": stop_evidence,
    }
    private_json(output / "manifest.json", manifest)
    check_set(engine, plan, initial, stopped)
    verify(output, sealed=False)
    check_set(engine, plan, initial, stopped)
    private_json(
        output / "complete.json",
        {"format": FORMAT, "manifest_sha256": digest(manifest)},
    )
    sync_directory(output)
    sync_directory(output.parent)
    return manifest


def verify(folder, sealed=True):
    require(folder.is_dir() and not folder.is_symlink(), "Expected a snapshot directory")
    manifest = read_json(folder / "manifest.json")
    require(
        isinstance(manifest, dict)
        and set(manifest)
        == {
            "format",
            "plan",
            "started_at",
            "quiesced_at",
            "completed_at",
            "duration_seconds",
            "archives",
            "stops",
        },
        "Unsupported snapshot manifest",
    )
    require(manifest["format"] in SUPPORTED_FORMATS, "Unsupported snapshot format")
    require(
        timestamp(manifest["started_at"])
        <= timestamp(manifest["quiesced_at"])
        <= timestamp(manifest["completed_at"]),
        "Invalid snapshot time ordering",
    )
    duration = manifest["duration_seconds"]
    require(
        type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
        "Invalid snapshot duration",
    )
    plan = validate_plan(manifest["plan"])
    require(plan["format"] == manifest["format"], "Snapshot format mismatch")
    require(read_json(folder / "plan.json") == plan, "Snapshot plan mismatch")
    require(set(manifest["archives"]) == set(PATHS), "Incomplete snapshot stores")
    require(
        set(manifest["stops"]) == {r["id"] for r in plan["containers"]},
        "Incomplete shutdown evidence",
    )
    expected = {"manifest.json", "plan.json"}
    for index, row in enumerate(sorted(plan["containers"], key=lambda r: ORDER[r["role"]])):
        name = f"stop-{index:02d}.json"
        stop = read_json(folder / name)
        require(
            set(stop) == {"id", "role", "started_at", "finished_at", "exit_code"}
            and stop["id"] == row["id"]
            and stop["role"] == row["role"]
            and normal_exit(row["role"], stop["exit_code"]),
            "Invalid shutdown evidence",
        )
        if manifest["format"] == LEGACY_FORMAT:
            require(
                stop["exit_code"] == 0 and stop["finished_at"] == manifest["stops"][row["id"]],
                "Invalid legacy shutdown evidence",
            )
        else:
            require(stop == manifest["stops"][row["id"]], "Shutdown evidence checksum mismatch")
        require(
            timestamp(stop["finished_at"]) >= timestamp(stop["started_at"]),
            "Invalid shutdown timestamps",
        )
        expected.add(name)
    for role in PATHS:
        name = f"{role}.tar"
        require(
            archive_info(folder / name) == manifest["archives"][role],
            "Archive checksum or structure mismatch",
        )
        expected.add(name)
    if sealed:
        require(
            read_json(folder / "complete.json")
            == {"format": manifest["format"], "manifest_sha256": digest(manifest)},
            "Snapshot completion seal mismatch",
        )
        expected.add("complete.json")
    require({p.name for p in folder.iterdir()} == expected, "Unexpected files in snapshot")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="CLI deadline; never forces container termination",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    prepare_args = actions.add_parser(
        "plan", help="Read-only: prepare the exact labelled container set"
    )
    prepare_args.add_argument("--set", dest="set_id", required=True)
    prepare_args.add_argument("--source-commit", required=True)
    prepare_args.add_argument("--output", type=Path, required=True)
    capture_args = actions.add_parser(
        "capture", help="Stop the prepared set and capture; leave services stopped"
    )
    capture_args.add_argument("--plan", type=Path, required=True)
    capture_args.add_argument("--output", type=Path, required=True)
    verify_args = actions.add_parser(
        "verify", help="Offline integrity verification; no container access"
    )
    verify_args.add_argument("snapshot", type=Path)
    stage_plan_args = actions.add_parser(
        "plan-stage", help="Read-only: select new isolated restore containers"
    )
    stage_plan_args.add_argument("--snapshot", type=Path, required=True)
    stage_plan_args.add_argument("--set", dest="set_id", required=True)
    stage_plan_args.add_argument("--output", type=Path, required=True)
    stage_args = actions.add_parser(
        "stage", help="Restore into new empty stores; never start services"
    )
    stage_args.add_argument("--snapshot", type=Path, required=True)
    stage_args.add_argument("--plan", type=Path, required=True)
    stage_args.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(1 <= args.timeout <= 86400, "Invalid CLI deadline")
        if args.action == "verify":
            verify(args.snapshot)
            print(
                "Snapshot integrity verified. "
                "This is not a restore rehearsal or an authenticity signature."
            )
        elif args.action == "plan":
            private_json(
                args.output,
                prepare(Engine(args.engine, args.timeout), args.set_id, args.source_commit),
            )
            print(
                "Plan saved. No services changed. "
                "Review the supported storage contract before capture."
            )
        elif args.action == "plan-stage":
            source = verify(args.snapshot)
            private_json(
                args.output,
                prepare(
                    Engine(args.engine, args.timeout),
                    args.set_id,
                    source["plan"]["source_commit"],
                    fresh=True,
                ),
            )
            print("Restore target plan saved. No services or stored bytes changed.")
        elif args.action == "stage":
            receipt = stage(
                Engine(args.engine, args.timeout),
                args.snapshot,
                read_json(args.plan),
                args.output,
            )
            print(
                f"Restore staged in {receipt['duration_seconds']}s. "
                "Services remain isolated and unstarted; "
                "generation rotation and rehearsal are still required."
            )
        else:
            manifest = capture(Engine(args.engine, args.timeout), read_json(args.plan), args.output)
            print(
                f"Snapshot sealed in {manifest['duration_seconds']}s. "
                "Selected services remain stopped."
            )
    except (
        SnapshotError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        tarfile.TarError,
    ) as exc:
        # No raw engine output, paths, inspect fields or payload exceptions.
        message = str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__
        parser.exit(
            1,
            f"Snapshot failed: {message}. No automatic restart or cleanup was performed.\n",
        )


if __name__ == "__main__":
    main()

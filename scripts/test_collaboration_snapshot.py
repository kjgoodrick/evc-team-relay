"""Snapshot protocol fault tests; no container daemon or live files are used."""

import copy
import io
import json
import subprocess
import tarfile
import uuid

import collaboration_snapshot as snapshot
import pytest

SET_ID = str(uuid.uuid4())
COMMIT = "a" * 40


def tar_bytes(entries=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for entry in entries or [("state", b"retained bytes")]:
            if isinstance(entry, tarfile.TarInfo):
                archive.addfile(entry)
            else:
                name, body = entry
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return stream.getvalue()


class FakeEngine:
    def __init__(self):
        self.rows = {}
        self.events = []
        self.after_stop = lambda role: None
        self.after_archive = lambda role: None
        self.complete = True
        self.store_bytes = {}
        self.after_restore = lambda role: None
        for index, role in enumerate(snapshot.ORDER):
            cid = f"{index + 1:064x}"
            self.rows[cid] = {
                "Id": cid,
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Labels": {snapshot.SET_LABEL: SET_ID, snapshot.ROLE_LABEL: role},
                    "Env": ["SYNTHETIC_SECRET=do-not-export"],
                },
                "HostConfig": {},
                "Mounts": (
                    [
                        {
                            "Type": "volume",
                            "RW": True,
                            "Destination": snapshot.PATHS[role],
                            "Source": f"/volumes/{role}",
                        }
                    ]
                    if role in snapshot.PATHS
                    else []
                ),
                "RestartCount": 0,
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-11-08T01:00:00Z",
                    "FinishedAt": "",
                    "ExitCode": 0,
                    "OOMKilled": False,
                },
            }

    def row(self, role):
        return next(
            r for r in self.rows.values() if r["Config"]["Labels"][snapshot.ROLE_LABEL] == role
        )

    def ids(self, set_id=None):
        return [
            cid
            for cid, row in self.rows.items()
            if set_id is None or row["Config"]["Labels"].get(snapshot.SET_LABEL) == set_id
        ]

    def inspect(self, cid):
        return copy.deepcopy(self.rows[cid])

    def stop(self, cid):
        row = self.rows[cid]
        role = row["Config"]["Labels"][snapshot.ROLE_LABEL]
        self.events.append(("stop", role))
        row["State"].update(Running=False, Status="exited", FinishedAt="2026-11-08T02:00:00Z")
        self.after_stop(role)

    def logs(self, cid, started_at):
        assert started_at == self.rows[cid]["State"]["StartedAt"]
        if cid == self.row("control-plane")["Id"]:
            return (
                b"control_plane_shutdown_complete\nApplication shutdown complete.\n"
                b"Finished server process [1]"
            )
        return (
            (snapshot.COMPLETION + "\nServer shut down.").encode() if self.complete else b"failed"
        )

    def archive(self, cid, path, destination):
        role = self.rows[cid]["Config"]["Labels"][snapshot.ROLE_LABEL]
        assert path == snapshot.PATHS[role]
        assert all(not r["State"]["Running"] for r in self.rows.values())
        self.events.append(("archive", role))
        destination.write_bytes(self.store_bytes.get(cid, tar_bytes()))
        self.after_archive(role)

    def restore(self, cid, path, source):
        role = self.rows[cid]["Config"]["Labels"][snapshot.ROLE_LABEL]
        assert path == snapshot.PATHS[role]
        self.events.append(("restore", role))
        self.store_bytes[cid] = source.read_bytes()
        self.after_restore(role)


@pytest.fixture
def engine():
    return FakeEngine()


def plan(engine):
    return snapshot.prepare(engine, SET_ID, COMMIT)


def test_capture_orders_all_writers_before_stores_and_seals_private_evidence(engine, tmp_path):
    selected = plan(engine)
    assert engine.events == []
    result = snapshot.capture(engine, selected, tmp_path / "capture")
    assert snapshot.verify(tmp_path / "capture") == result
    assert engine.events[:6] == [("stop", role) for role in snapshot.ORDER]
    assert len(engine.events) == 10
    assert set(result["archives"]) == set(snapshot.PATHS)
    assert all(row["configuration"] for row in selected["containers"])
    assert (tmp_path / "capture").stat().st_mode & 0o777 == 0o700
    for path in (tmp_path / "capture").glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert "do-not-export" not in path.read_text()
        assert "SYNTHETIC_SECRET" not in path.read_text()


@pytest.mark.parametrize("change", ["image", "config", "membership", "role", "stopped"])
def test_stale_plan_never_stops_anything(engine, tmp_path, change):
    selected = plan(engine)
    row = engine.row("relay")
    if change == "image":
        row["Image"] = "c" * 64
    elif change == "config":
        row["Config"]["Env"] = ["different"]
    elif change == "membership":
        del engine.rows[row["Id"]]
    elif change == "role":
        row["Config"]["Labels"][snapshot.ROLE_LABEL] = "worker"
    else:
        row["State"]["Running"] = False
    with pytest.raises(snapshot.SnapshotError):
        snapshot.capture(engine, selected, tmp_path / "capture")
    assert engine.events == []
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize(
    "failure", ["exit", "oom", "error", "restart", "running", "missing-completion"]
)
def test_failed_drain_never_captures_or_restarts(engine, tmp_path, failure):
    selected = plan(engine)

    def fail(role):
        if role != "relay":
            return
        row = engine.row(role)
        if failure == "exit":
            row["State"]["ExitCode"] = 137
        elif failure == "oom":
            row["State"]["OOMKilled"] = True
        elif failure == "error":
            row["State"]["Error"] = "failed"
        elif failure == "restart":
            row["RestartCount"] += 1
        elif failure == "running":
            row["State"]["Running"] = True
        else:
            engine.complete = False

    engine.after_stop = fail
    with pytest.raises(snapshot.SnapshotError):
        snapshot.capture(engine, selected, tmp_path / "capture")
    assert all(event[0] == "stop" for event in engine.events)
    assert not (tmp_path / "capture/complete.json").exists()
    assert engine.row("postgres")["State"]["Running"]


@pytest.mark.parametrize("role", list(snapshot.PATHS))
def test_restart_during_each_copy_leaves_unsealed_evidence(engine, tmp_path, role):
    def restart(copied):
        if copied == role:
            engine.row("control-plane")["State"]["StartedAt"] = "2026-11-08T03:00:00Z"

    engine.after_archive = restart
    with pytest.raises(snapshot.SnapshotError, match="restarted"):
        snapshot.capture(engine, plan(engine), tmp_path / "capture")
    assert (tmp_path / "capture" / f"{role}.tar").exists()
    assert not (tmp_path / "capture/complete.json").exists()


def test_restart_during_final_verification_does_not_seal(engine, tmp_path, monkeypatch):
    real_verify = snapshot.verify

    def verify_then_restart(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        engine.row("relay")["RestartCount"] += 1
        return result

    monkeypatch.setattr(snapshot, "verify", verify_then_restart)
    with pytest.raises(snapshot.SnapshotError, match="restarted"):
        snapshot.capture(engine, plan(engine), tmp_path / "capture")
    assert not (tmp_path / "capture/complete.json").exists()


@pytest.mark.parametrize(
    "conflict",
    ["extra-writer", "extra-mount", "overlap", "outside-volume", "outside-parent"],
)
def test_uncovered_or_shared_storage_rejected_before_shutdown(engine, conflict):
    if conflict == "extra-writer":
        engine.row("worker")["Mounts"] = copy.deepcopy(engine.row("relay")["Mounts"])
    elif conflict == "extra-mount":
        engine.row("relay")["Mounts"] *= 2
    elif conflict == "overlap":
        engine.row("relay")["Mounts"][0]["Source"] = "/volumes/objects/nested"
    else:
        outside = copy.deepcopy(engine.row("objects"))
        outside["Id"] = "f" * 64
        outside["Config"]["Labels"] = {}
        if conflict == "outside-parent":
            outside["Mounts"][0]["Source"] = "/volumes"
        engine.rows[outside["Id"]] = outside
    with pytest.raises(snapshot.SnapshotError):
        plan(engine)
    assert engine.events == []


def test_no_overwrite_existing_snapshot(engine, tmp_path):
    target = tmp_path / "capture"
    target.mkdir()
    (target / "preserve").write_text("original")
    with pytest.raises(FileExistsError):
        snapshot.capture(engine, plan(engine), target)
    assert (target / "preserve").read_text() == "original"
    assert engine.events == []


@pytest.mark.parametrize(
    "corruption", ["bytes", "missing", "seal", "extra", "symlink", "stop", "plan"]
)
def test_verify_rejects_incomplete_or_changed_backup(engine, tmp_path, corruption):
    target = tmp_path / "capture"
    snapshot.capture(engine, plan(engine), target)
    if corruption == "bytes":
        raw = bytearray((target / "objects.tar").read_bytes())
        raw[1024] ^= 1
        (target / "objects.tar").write_bytes(raw)
    elif corruption == "missing":
        (target / "objects.tar").unlink()
    elif corruption == "seal":
        (target / "complete.json").write_text("{}")
    elif corruption == "extra":
        (target / "unlisted").write_bytes(b"other")
    elif corruption == "symlink":
        (target / "objects.tar").rename(tmp_path / "external.tar")
        (target / "objects.tar").symlink_to(tmp_path / "external.tar")
    elif corruption == "stop":
        data = json.loads((target / "stop-00.json").read_text())
        data["exit_code"] = 137
        (target / "stop-00.json").write_text(json.dumps(data))
    else:
        data = json.loads((target / "plan.json").read_text())
        data["source_commit"] = "f" * 40
        (target / "plan.json").write_text(json.dumps(data))
    with pytest.raises((snapshot.SnapshotError, tarfile.TarError)):
        snapshot.verify(target)


@pytest.mark.parametrize(
    "kind",
    [
        "absolute",
        "parent",
        "link",
        "hardlink",
        "device",
        "duplicate",
        "ancestor",
        "reverse-ancestor",
        "trailing",
        "truncated",
    ],
)
def test_tar_validation_rejects_unsafe_or_hidden_content(tmp_path, kind):
    if kind == "absolute":
        entries = [("/outside", b"bad")]
    elif kind == "parent":
        entries = [("safe/../outside", b"bad")]
    elif kind in ("link", "hardlink", "device"):
        entry = tarfile.TarInfo("unsafe")
        entry.type = {
            "link": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "device": tarfile.CHRTYPE,
        }[kind]
        entry.linkname = "/outside"
        entries = [entry]
    elif kind == "duplicate":
        entries = [("same", b"one"), ("./same", b"two")]
    elif kind == "ancestor":
        entries = [("file", b"one"), ("file/child", b"two")]
    elif kind == "reverse-ancestor":
        entries = [("file/child", b"two"), ("file", b"one")]
    else:
        entries = None
    raw = tar_bytes(entries)
    if kind == "trailing":
        raw += tar_bytes()
    elif kind == "truncated":
        raw = raw[:1536]
    path = tmp_path / "bad.tar"
    path.write_bytes(raw)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.archive_info(path)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"format": "first", "format": "second"}')
    with pytest.raises(snapshot.SnapshotError, match="Duplicate"):
        snapshot.read_json(path)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_is_rejected(tmp_path, value):
    path = tmp_path / "bad.json"
    path.write_text('{"duration": ' + value + "}")
    with pytest.raises(snapshot.SnapshotError, match="Nonfinite"):
        snapshot.read_json(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("started_at", "0001-01-01T00:00:00Z"),
        ("completed_at", "1970-01-01T00:00:00Z"),
        ("quiesced_at", "2026-11-08"),
        ("duration_seconds", -1),
        ("duration_seconds", False),
    ],
)
def test_invalid_snapshot_timing_is_rejected_even_with_matching_seal(
    engine, tmp_path, field, value
):
    source = captured(engine, tmp_path)
    manifest = snapshot.read_json(source / "manifest.json")
    manifest[field] = value
    (source / "manifest.json").write_bytes(snapshot.encoded(manifest))
    (source / "complete.json").write_bytes(
        snapshot.encoded(
            {
                "format": snapshot.FORMAT,
                "manifest_sha256": snapshot.digest(manifest),
            }
        )
    )
    with pytest.raises(snapshot.SnapshotError):
        snapshot.verify(source)


def test_engine_stop_is_infinite_and_cli_timeout_does_not_issue_kill(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args, 15, stderr=b"SYNTHETIC_SECRET")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(snapshot.SnapshotError) as error:
        snapshot.Engine("podman", 15).stop("a" * 64)
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert calls == [["podman", "stop", "--time", "-1", "a" * 64]]


def test_engine_failure_does_not_disclose_stderr(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b"secret"),
    )
    with pytest.raises(snapshot.SnapshotError) as error:
        snapshot.Engine().ids()
    assert "secret" not in str(error.value)


def test_engine_logs_collects_both_streams(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"stdout", b"stderr"),
    )
    assert snapshot.Engine().logs("a" * 64, "2026-11-08T01:00:00Z") == b"stdoutstderr"


@pytest.mark.parametrize("code", [0, 130, 143])
def test_completed_control_plane_signal_exit_is_recorded_exactly(engine, tmp_path, code):
    def stop(role):
        if role == "control-plane":
            engine.row(role)["State"]["ExitCode"] = code

    engine.after_stop = stop
    output = tmp_path / "capture"
    manifest = snapshot.capture(engine, plan(engine), output)
    cid = engine.row("control-plane")["Id"]
    assert manifest["stops"][cid]["exit_code"] == code
    assert snapshot.verify(output) == manifest


@pytest.mark.parametrize(
    "code,log",
    [
        (0, b"Finished server process [1]"),
        (143, b"Application shutdown failed.\nFinished server process [1]"),
        (143, b"Application shutdown complete."),
        (137, b"Application shutdown complete.\nFinished server process [1]"),
        (1, b"Application shutdown complete.\nFinished server process [1]"),
        (0, b"Application shutdown complete.\nFinished server process [1]"),
        (
            143,
            b"control_plane_shutdown_complete\nApplication shutdown complete.\n"
            b"Finished server process [1]\nASGI 'lifespan' protocol appears unsupported.",
        ),
        (
            143,
            b"control_plane_shutdown_complete\nApplication shutdown complete.\n"
            b"Finished server process [1]\nException in 'lifespan' protocol",
        ),
    ],
)
def test_incomplete_or_forced_control_plane_shutdown_never_captures(engine, tmp_path, code, log):
    normal_logs = engine.logs

    def logs(cid, started):
        return log if cid == engine.row("control-plane")["Id"] else normal_logs(cid, started)

    def stop(role):
        if role == "control-plane":
            engine.row(role)["State"]["ExitCode"] = code

    engine.logs = logs
    engine.after_stop = stop
    with pytest.raises(snapshot.SnapshotError):
        snapshot.capture(engine, plan(engine), tmp_path / "capture")
    assert not any(event[0] == "archive" for event in engine.events)
    assert not (tmp_path / "capture/complete.json").exists()


def test_stop_receipt_timestamps_are_bound_into_the_completion_hash(engine, tmp_path):
    source = captured(engine, tmp_path)
    stop = snapshot.read_json(source / "stop-00.json")
    stop["started_at"] = "2026-11-08T00:00:00Z"
    (source / "stop-00.json").write_bytes(snapshot.encoded(stop))
    with pytest.raises(snapshot.SnapshotError, match="checksum"):
        snapshot.verify(source)


def test_legacy_snapshot_remains_verifiable_and_can_stage_into_current_format(engine, tmp_path):
    source = captured(engine, tmp_path)
    manifest = snapshot.read_json(source / "manifest.json")
    manifest["format"] = manifest["plan"]["format"] = snapshot.LEGACY_FORMAT
    manifest["stops"] = {cid: record["finished_at"] for cid, record in manifest["stops"].items()}
    (source / "plan.json").write_bytes(snapshot.encoded(manifest["plan"]))
    (source / "manifest.json").write_bytes(snapshot.encoded(manifest))
    (source / "complete.json").write_bytes(
        snapshot.encoded(
            {
                "format": snapshot.LEGACY_FORMAT,
                "manifest_sha256": snapshot.digest(manifest),
            }
        )
    )
    assert snapshot.verify(source) == manifest
    target, scope = fresh_target()
    selected = snapshot.prepare(target, scope, COMMIT, fresh=True)
    assert (
        snapshot.stage(target, source, selected, tmp_path / "staging")["format"] == snapshot.FORMAT
    )


def test_old_prepared_plan_requires_fresh_readonly_planning(engine, tmp_path):
    selected = plan(engine)
    selected["format"] = snapshot.LEGACY_FORMAT
    with pytest.raises(snapshot.SnapshotError, match="stale"):
        snapshot.capture(engine, selected, tmp_path / "capture")
    assert engine.events == []


def fresh_target():
    engine = FakeEngine()
    scope = str(uuid.uuid4())
    rows = {}
    for index, row in enumerate(engine.rows.values()):
        row["Id"] = f"{index + 100:064x}"
        row["Config"]["Labels"][snapshot.SET_LABEL] = scope
        row["HostConfig"]["NetworkMode"] = "none"
        row["State"].update(Running=False, Status="created", StartedAt="0001-01-01T00:00:00Z")
        for mount in row["Mounts"]:
            mount["Source"] += "-target"
        rows[row["Id"]] = row
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
        engine.store_bytes[row["Id"]] = stream.getvalue()
    engine.rows = rows
    return engine, scope


def captured(engine, tmp_path):
    source = tmp_path / "capture"
    snapshot.capture(engine, plan(engine), source)
    return source


def test_restore_stages_only_new_empty_isolated_stores_and_verifies_readback(engine, tmp_path):
    source = captured(engine, tmp_path)
    target, scope = fresh_target()
    selected = snapshot.prepare(target, scope, COMMIT, fresh=True)
    receipt = snapshot.stage(target, source, selected, tmp_path / "staging")
    assert receipt["state"] == "staged-offline-requires-generation-rotation-and-rehearsal"
    assert {event[0] for event in target.events} == {"archive", "restore"}
    assert len([event for event in target.events if event[0] == "restore"]) == 4
    assert all(snapshot.fresh_isolated(row) for row in target.rows.values())
    assert snapshot.read_json(tmp_path / "staging/staged.json") == receipt
    assert snapshot.verify(source)


@pytest.mark.parametrize(
    "failure",
    ["nonempty", "image", "revision", "running", "network", "reused-id", "reused-set"],
)
def test_restore_preflight_failure_does_not_write_any_store(engine, tmp_path, failure):
    source = captured(engine, tmp_path)
    target, scope = fresh_target()
    if failure == "nonempty":
        target.store_bytes[target.row("objects")["Id"]] = tar_bytes()
    elif failure == "image":
        target.row("objects")["Image"] = "e" * 64
    elif failure == "reused-id":
        old_id = target.row("objects")["Id"]
        row = target.rows.pop(old_id)
        row["Id"] = engine.row("objects")["Id"]
        target.rows[row["Id"]] = row
    elif failure == "reused-set":
        scope = SET_ID
        for row in target.rows.values():
            row["Config"]["Labels"][snapshot.SET_LABEL] = scope
    selected = snapshot.prepare(
        target, scope, "b" * 40 if failure == "revision" else COMMIT, fresh=True
    )
    if failure == "running":
        target.row("relay")["State"].update(Running=True, Status="running")
    elif failure == "network":
        target.row("objects")["HostConfig"]["NetworkMode"] = "host"
    with pytest.raises(snapshot.SnapshotError):
        snapshot.stage(target, source, selected, tmp_path / "staging")
    assert not any(event[0] == "restore" for event in target.events)
    assert not (tmp_path / "staging/staged.json").exists()


@pytest.mark.parametrize("failure", ["corrupt", "mode", "owner", "started", "copy-error"])
def test_restore_refuses_changed_readback_and_keeps_partial_new_stores_closed(
    engine, tmp_path, failure
):
    source = captured(engine, tmp_path)
    target, scope = fresh_target()
    selected = snapshot.prepare(target, scope, COMMIT, fresh=True)

    def fail(role):
        row = target.row(role)
        if failure == "copy-error":
            raise snapshot.SnapshotError("Synthetic incomplete copy")
        if failure == "started":
            row["State"].update(Running=True, Status="running")
        elif failure == "corrupt":
            target.store_bytes[row["Id"]] = tar_bytes([("state", b"different")])
        else:
            stream = io.BytesIO()
            with (
                tarfile.open(fileobj=io.BytesIO(target.store_bytes[row["Id"]])) as original,
                tarfile.open(fileobj=stream, mode="w") as altered,
            ):
                for entry in original:
                    if failure == "mode":
                        entry.mode = 0o777
                    else:
                        entry.uid = 1234
                    altered.addfile(entry, original.extractfile(entry) if entry.isreg() else None)
            target.store_bytes[row["Id"]] = stream.getvalue()

    target.after_restore = fail
    with pytest.raises(snapshot.SnapshotError):
        snapshot.stage(target, source, selected, tmp_path / "staging")
    assert len([event for event in target.events if event[0] == "restore"]) == 1
    assert not (tmp_path / "staging/staged.json").exists()
    assert snapshot.verify(source)


def test_semantic_tree_hash_is_independent_of_tar_entry_order(tmp_path):
    one, two = tmp_path / "one.tar", tmp_path / "two.tar"
    one.write_bytes(tar_bytes([("a", b"one"), ("b", b"two")]))
    two.write_bytes(tar_bytes([("b", b"two"), ("a", b"one")]))
    first, second = snapshot.archive_info(one), snapshot.archive_info(two)
    assert first["tree_sha256"] == second["tree_sha256"]
    assert first["sha256"] != second["sha256"]

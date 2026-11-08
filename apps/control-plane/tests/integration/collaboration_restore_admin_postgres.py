"""Exercise migrations, offline rotation CLI and lock races on a new PostgreSQL fixture.

This script creates/removes only its own named container. Staging metadata is a
synthetic protocol fixture, not an assertion of a complete application restore.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db import models as m
from app.maintenance import collaboration_restore as cli
from app.services import collaboration_restore_admin as admin

VERSIONS = ("202611080001",)


def run(args, *, env=None, expected=0):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, timeout=90, check=False)
    if result.returncode != expected:
        raise AssertionError(
            f"Disposable command returned {result.returncode}; expected {expected}"
        )
    return result.stdout.decode().strip()


def write_evidence(folder):
    folder.mkdir()
    plans = []
    for offset in (1, 100):
        plans.append(
            {
                "format": cli.FORMAT,
                "storage_contract": "single-postgres-minio-local-relay-v1",
                "set_id": str(uuid.uuid4()),
                "source_commit": "a" * 40,
                "containers": [
                    {"id": f"{offset + index:064x}", "role": role}
                    for index, role in enumerate(sorted(cli.STORE_ROLES))
                ],
            }
        )
    source = {"format": cli.FORMAT, "plan": plans[0]}
    target = plans[1]
    staged = {
        "format": cli.FORMAT,
        "state": "staged-offline-requires-generation-rotation-and-rehearsal",
        "source_manifest_sha256": admin.checksum(source),
        "target_plan_sha256": admin.checksum(target),
    }
    for name, value in (
        ("source-manifest.json", source),
        ("target-plan.json", target),
        ("staged.json", staged),
    ):
        (folder / name).write_text(json.dumps(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument("--postgres-image", required=True)
    args = parser.parse_args()
    name = "evc-restore-admin-" + str(uuid.uuid4())
    cid = None
    engine = None
    before = run([args.engine, "ps", "-aq", "--no-trunc"]).split()
    with tempfile.TemporaryDirectory(prefix="evc-restore-admin-proof-") as temp:
        folder = Path(temp)
        try:
            cid = run(
                [
                    args.engine,
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    name,
                    "--restart=no",
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    "POSTGRES_USER=fixture",
                    "-e",
                    "POSTGRES_DB=collaboration_test",
                    "-e",
                    "POSTGRES_PASSWORD=disposable-restore-admin-password",
                    args.postgres_image,
                ]
            )
            deadline = time.monotonic() + 30
            while True:
                probe = subprocess.run(
                    [
                        args.engine,
                        "exec",
                        cid,
                        "pg_isready",
                        "-U",
                        "fixture",
                        "-d",
                        "collaboration_test",
                    ],
                    capture_output=True,
                    check=False,
                )
                if probe.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("Disposable PostgreSQL was not ready")
                time.sleep(0.1)
            mapping = run([args.engine, "port", cid, "5432/tcp"])
            assert mapping.startswith("127.0.0.1:") and "\n" not in mapping
            url = f"postgresql+psycopg://fixture:disposable-restore-admin-password@{mapping}/collaboration_test"
            env = {**os.environ, "DATABASE_URL": url}
            run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
            run([sys.executable, "-m", "alembic", "downgrade", "202611070005"], env=env)
            run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
            engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
            with Session(engine) as db:
                user = m.User(email="owner@rotation-fixture.test", password_hash="", is_active=True)
                db.add(user)
                db.flush()
                db.add_all(
                    [
                        m.Share(
                            kind=m.ShareKind.FOLDER,
                            path=f"Fixture-{i}",
                            managed=True,
                            owner_user_id=user.id,
                            visibility=m.ShareVisibility.PRIVATE,
                            collaboration_sequence=17 + i,
                            collaboration_epoch=uuid.uuid4(),
                            collaboration_restore_generation=uuid.uuid4() if i else None,
                        )
                        for i in range(2)
                    ]
                )
                db.commit()
                original = admin.inventory(db)
            credential = folder / "database-url"
            credential.write_text(url)
            credential.chmod(0o600)
            evidence = folder / "staging"
            write_evidence(evidence)
            # A role without visibility into other sessions must not treat a
            # redacted activity view as proof that it is alone.
            with engine.begin() as db:
                db.execute(
                    text("CREATE ROLE limited_restore LOGIN PASSWORD 'disposable-limited-password'")
                )
            limited = folder / "limited-url"
            limited.write_text(
                f"postgresql+psycopg://limited_restore:disposable-limited-password@{mapping}/collaboration_test"
            )
            limited.chmod(0o600)
            run(
                [
                    sys.executable,
                    "-m",
                    "app.maintenance.collaboration_restore",
                    "--database-url-file",
                    str(limited),
                    "plan",
                    "--staging-evidence",
                    str(evidence),
                    "--operator",
                    "Restricted fixture",
                    "--output",
                    str(folder / "limited-plan.json"),
                ],
                expected=1,
            )
            assert (folder / "limited-plan.json").read_bytes() == b""
            command = [
                sys.executable,
                "-m",
                "app.maintenance.collaboration_restore",
                "--database-url-file",
                str(credential),
            ]
            plan_path = folder / "plan.json"
            run(
                [
                    *command,
                    "plan",
                    "--staging-evidence",
                    str(evidence),
                    "--operator",
                    "Disposable PostgreSQL proof",
                    "--output",
                    str(plan_path),
                ]
            )
            selected = admin.RestorePlan.model_validate_json(plan_path.read_text())
            with engine.connect():
                run(
                    [
                        *command,
                        "apply",
                        "--staging-evidence",
                        str(evidence),
                        "--plan",
                        str(plan_path),
                        "--output",
                        str(folder / "blocked.json"),
                    ],
                    expected=1,
                )
            assert (folder / "blocked.json").read_bytes() == b""
            with Session(engine) as db:
                assert admin.inventory(db) == original
            receipt_path = folder / "applied.json"
            run(
                [
                    *command,
                    "apply",
                    "--staging-evidence",
                    str(evidence),
                    "--plan",
                    str(plan_path),
                    "--output",
                    str(receipt_path),
                ]
            )
            assert json.loads(receipt_path.read_text())["state"] == "applied"
            receipt_path.unlink()  # Synthetic lost result; the SQL receipt survives.
            found = json.loads(run([*command, "receipt", "--plan", str(plan_path)]))
            assert found["state"] == "recorded" and found["share_count"] == 2
            run(
                [
                    *command,
                    "apply",
                    "--staging-evidence",
                    str(evidence),
                    "--plan",
                    str(plan_path),
                    "--output",
                    str(folder / "retry.json"),
                ]
            )
            assert json.loads((folder / "retry.json").read_text())["state"] == "already-applied"
            with Session(engine) as db:
                assert all(item.generation == selected.generation for item in admin.inventory(db))
                assert [item.sequence for item in admin.inventory(db)] == [
                    item.sequence for item in original
                ]
                assert db.scalar(select(func.count()).select_from(m.CollaborationRestore)) == 1
                assert db.scalar(select(func.count()).select_from(m.AuditLog)) == 2
                first = admin.prepare(db, "c" * 64, "d" * 64, "First concurrent plan", VERSIONS)
                second = admin.prepare(db, "c" * 64, "d" * 64, "Second concurrent plan", VERSIONS)
            pids = Queue()

            def waiting():
                with Session(engine) as db, db.begin():
                    pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                    try:
                        admin.apply(db, second, VERSIONS)
                    except admin.RestoreError:
                        return "rejected"
                    raise AssertionError("Competing stale restore plan was applied")

            with ThreadPoolExecutor(max_workers=1) as pool:
                with Session(engine) as db:
                    db.execute(
                        text("LOCK TABLE shares, collaboration_restores IN ACCESS EXCLUSIVE MODE")
                    )
                    future = pool.submit(waiting)
                    pid = pids.get(timeout=5)
                    deadline = time.monotonic() + 4
                    while True:
                        with engine.connect() as probe:
                            wait = probe.scalar(
                                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                                {"pid": pid},
                            )
                        if wait == "Lock":
                            break
                        if time.monotonic() >= deadline:
                            raise AssertionError(
                                "Competing rotation did not wait for the metadata lock"
                            )
                        time.sleep(0.01)
                    admin.apply(db, first, VERSIONS)
                    db.commit()
                assert future.result(timeout=10) == "rejected"
            with Session(engine) as db:
                assert admin.apply(db, selected, VERSIONS)["state"] == "superseded"
                db.commit()
                assert all(item.generation == first.generation for item in admin.inventory(db))
                assert db.scalar(select(func.count()).select_from(m.CollaborationRestore)) == 2
            # Once used, migration rollback must preserve its durable evidence.
            run([sys.executable, "-m", "alembic", "downgrade", "202611070005"], env=env, expected=1)
            with Session(engine) as db:
                assert cli.versions(db) == VERSIONS
                assert db.scalar(select(func.count()).select_from(m.CollaborationRestore)) == 2
            print(
                json.dumps(
                    {
                        "passed": True,
                        "migration_roundtrip": True,
                        "connected_client_rejected": True,
                        "restricted_session_visibility_rejected": True,
                        "cli_rotation_and_receipt": True,
                        "lost_result_retry": True,
                        "competing_plan_lock_wait": True,
                        "superseded_plan_read_only": True,
                        "used_receipt_downgrade_rejected": True,
                    }
                )
            )
        finally:
            if engine is not None:
                engine.dispose()
            if cid is not None:
                run([args.engine, "rm", "-f", "--volumes", cid])
    assert sorted(run([args.engine, "ps", "-aq", "--no-trunc"]).split()) == sorted(before)


if __name__ == "__main__":
    main()

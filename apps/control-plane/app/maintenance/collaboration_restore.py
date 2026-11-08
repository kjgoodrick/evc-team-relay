"""Prepare/apply/inspect restore lineage on an isolated, staged PostgreSQL copy.

No defaults discover live credentials. No HTTP service, workers or network
configuration are started. See docs/collaboration-restore-rotation.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db import models as m
from app.services import collaboration_restore_admin as admin

MAX_METADATA = 4 * 1024 * 1024
FORMAT = "evc-cold-snapshot-v2"
SUPPORTED_FORMATS = {FORMAT, "evc-cold-snapshot-v1"}
STORE_ROLES = {"postgres", "objects", "relay", "control-plane"}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise admin.RestoreError(message)


def read_json(path: Path):
    require(path.is_file() and not path.is_symlink(), "Expected regular restore metadata")
    with path.open("rb") as stream:
        raw = stream.read(MAX_METADATA + 1)
    require(len(raw) <= MAX_METADATA, "Restore metadata exceeds its limit")

    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate restore metadata key")
            result[key] = value
        return result

    def invalid_number(_value):
        raise admin.RestoreError("Invalid restore metadata number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_number)


def evidence(folder: Path) -> tuple[str, str]:
    require(folder.is_dir() and not folder.is_symlink(), "Expected a staging evidence directory")
    source = read_json(folder / "source-manifest.json")
    target = read_json(folder / "target-plan.json")
    staged = read_json(folder / "staged.json")
    for plan in (source["plan"], target):
        require(
            plan["format"] in SUPPORTED_FORMATS
            and plan["storage_contract"] == "single-postgres-minio-local-relay-v1"
            and str(uuid.UUID(plan["set_id"])) == plan["set_id"]
            and re.fullmatch(r"[a-f0-9]{40}", plan["source_commit"]) is not None,
            "Invalid staged container plan",
        )
        rows = plan["containers"]
        require(
            isinstance(rows, list) and 4 <= len(rows) <= 32, "Invalid staged container inventory"
        )
        ids, roles = [], []
        for row in rows:
            require(
                re.fullmatch(r"[a-f0-9]{64}", row["id"]) is not None
                and row["role"] in STORE_ROLES | {"worker", "ingress"},
                "Invalid staged container identity",
            )
            ids.append(row["id"])
            roles.append(row["role"])
        require(
            len(set(ids)) == len(ids) and all(roles.count(role) == 1 for role in STORE_ROLES),
            "Ambiguous staged storage roles",
        )
    snapshot_hash, target_hash = admin.checksum(source), admin.checksum(target)
    require(
        source["format"] in SUPPORTED_FORMATS
        and source["plan"]["format"] == source["format"]
        and target["format"] == staged["format"] == FORMAT,
        "Unsupported staging evidence",
    )
    require(
        staged["state"] == "staged-offline-requires-generation-rotation-and-rehearsal",
        "Restore staging is incomplete",
    )
    require(
        staged["source_manifest_sha256"] == snapshot_hash
        and staged["target_plan_sha256"] == target_hash,
        "Staging evidence mismatch",
    )
    require(
        source["plan"]["set_id"] != target["set_id"]
        and source["plan"]["source_commit"] == target["source_commit"],
        "Staging identity mismatch",
    )
    source_ids = {row["id"] for row in source["plan"]["containers"]}
    target_ids = {row["id"] for row in target["containers"]}
    require(not source_ids.intersection(target_ids), "Staging reused source containers")
    return snapshot_hash, target_hash


def url_from_file(path: Path):
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
        "Database credential file must be a private regular file",
    )
    with path.open("r") as stream:
        raw = stream.read(8193)
    require(len(raw) <= 8192, "Database credential exceeds its limit")
    url = make_url(raw.strip())
    require(
        url.drivername == "postgresql+psycopg" and bool(url.database),
        "Maintenance requires an explicit PostgreSQL psycopg URL",
    )
    return url


def versions(db: Session) -> tuple[str, ...]:
    actual = tuple(sorted(db.scalars(text("SELECT version_num FROM alembic_version"))))
    expected = tuple(
        sorted(ScriptDirectory(str(Path(__file__).parents[1] / "db/migrations")).get_heads())
    )
    require(actual == expected, "Database schema does not match this maintenance build")
    return actual


def isolated_database(db: Session) -> None:
    # An additional tripwire, not proof of network isolation. The maintenance
    # operator must keep clients and workers disconnected throughout.
    visible = db.scalar(
        text(
            "SELECT usesuper OR pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') "
            "FROM pg_user WHERE usename = current_user"
        )
    )
    require(visible is True, "Maintenance role requires visibility into all database sessions")
    others = db.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
        )
    )
    require(others == 0, "Restored database has other connected clients")
    prepared = db.scalar(
        text("SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()")
    )
    require(prepared == 0, "Restored database contains unresolved prepared transactions")


def write_json(stream, value) -> None:
    stream.write((json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())
    stream.flush()
    os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("plan", help="Read-only plan from isolated restored shares")
    prepare.add_argument("--staging-evidence", type=Path, required=True)
    prepare.add_argument("--operator", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    apply = actions.add_parser(
        "apply", help="Atomically rotate generations using exactly this plan"
    )
    apply.add_argument("--staging-evidence", type=Path, required=True)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    status = actions.add_parser(
        "receipt", help="Read-only exact-plan receipt; no rotation or locks"
    )
    status.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    engine = None
    try:
        url = url_from_file(args.database_url_file)
        engine = create_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=30000 -c application_name=evc_restore_maintenance",
            },
        )
        if args.action == "receipt":
            plan = admin.RestorePlan.model_validate(read_json(args.plan))
            with Session(engine) as db:
                receipt = db.scalar(
                    select(m.CollaborationRestore).where(
                        m.CollaborationRestore.generation == plan.generation
                    )
                )
                if receipt is None:
                    result = {"state": "unknown", "generation": str(plan.generation)}
                else:
                    admin.verify_receipt(receipt, plan)
                    result = {
                        "state": "recorded",
                        "generation": str(plan.generation),
                        "share_count": receipt.share_count,
                        "plan_sha256": receipt.plan_sha256,
                    }
            print(json.dumps(result, sort_keys=True))
            return
        snapshot_hash, target_hash = evidence(args.staging_evidence)
        plan = None
        if args.action == "apply":
            plan = admin.RestorePlan.model_validate(read_json(args.plan))
            require(
                plan.snapshot_sha256 == snapshot_hash and plan.target_sha256 == target_hash,
                "Plan belongs to different staged data",
            )
        # Reserve evidence before admitting a mutation. If a later commit/write
        # response is lost, retry or inspect this exact plan.
        with os.fdopen(
            os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as stream:
            with Session(engine) as db, db.begin():
                isolated_database(db)
                schema = versions(db)
                if plan is None:
                    plan = admin.prepare(db, snapshot_hash, target_hash, args.operator, schema)
                    result = plan.model_dump(mode="json")
                else:
                    result = admin.apply(db, plan, schema)
                    isolated_database(db)
            write_json(stream, result)
        print(
            "Restore maintenance result saved. "
            "Services remain isolated; no activation was performed."
        )
    except Exception as exc:
        # Validation/database/driver errors may include credentials or values.
        message = str(exc) if isinstance(exc, admin.RestoreError) else type(exc).__name__
        parser.exit(
            1,
            f"Restore maintenance failed: {message}. "
            "A database commit may already have succeeded; inspect or retry the same plan.\n",
        )
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()

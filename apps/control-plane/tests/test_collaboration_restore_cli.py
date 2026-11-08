import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.maintenance import collaboration_restore as cli
from app.services import collaboration_restore_admin as admin


def staging(folder):
    folder.mkdir()
    source = {
        "format": cli.FORMAT,
        "plan": {
            "set_id": str(uuid.uuid4()),
            "source_commit": "a" * 40,
            "format": cli.FORMAT,
            "storage_contract": "single-postgres-minio-local-relay-v1",
            "containers": [
                {"id": f"{index + 1:064x}", "role": role}
                for index, role in enumerate(sorted(cli.STORE_ROLES))
            ],
        },
    }
    target = {
        "format": cli.FORMAT,
        "set_id": str(uuid.uuid4()),
        "source_commit": "a" * 40,
        "storage_contract": "single-postgres-minio-local-relay-v1",
        "containers": [
            {"id": f"{index + 100:064x}", "role": role}
            for index, role in enumerate(sorted(cli.STORE_ROLES))
        ],
    }
    receipt = {
        "format": cli.FORMAT,
        "state": "staged-offline-requires-generation-rotation-and-rehearsal",
        "source_manifest_sha256": admin.checksum(source),
        "target_plan_sha256": admin.checksum(target),
    }
    for name, data in (
        ("source-manifest.json", source),
        ("target-plan.json", target),
        ("staged.json", receipt),
    ):
        (folder / name).write_text(json.dumps(data))
    return source, target, receipt


def test_evidence_uses_snapshot_tool_canonical_hashes(tmp_path):
    source, target, _ = staging(tmp_path / "evidence")
    assert cli.evidence(tmp_path / "evidence") == (admin.checksum(source), admin.checksum(target))


@pytest.mark.parametrize("legacy", [False, True])
def test_actual_snapshot_staging_metadata_is_accepted_without_reencoding_changes(
    tmp_path, monkeypatch, legacy
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    import collaboration_snapshot as snapshots
    from test_collaboration_snapshot import COMMIT, SET_ID, FakeEngine, fresh_target

    source_engine = FakeEngine()
    source = tmp_path / "source"
    captured = snapshots.capture(
        source_engine, snapshots.prepare(source_engine, SET_ID, COMMIT), source
    )
    if legacy:
        captured["format"] = captured["plan"]["format"] = snapshots.LEGACY_FORMAT
        captured["stops"] = {cid: value["finished_at"] for cid, value in captured["stops"].items()}
        (source / "plan.json").write_bytes(snapshots.encoded(captured["plan"]))
        (source / "manifest.json").write_bytes(snapshots.encoded(captured))
        (source / "complete.json").write_bytes(
            snapshots.encoded(
                {
                    "format": snapshots.LEGACY_FORMAT,
                    "manifest_sha256": snapshots.digest(captured),
                }
            )
        )
    target_engine, target_id = fresh_target()
    target = snapshots.prepare(target_engine, target_id, COMMIT, fresh=True)
    snapshots.stage(target_engine, source, target, tmp_path / "staged")
    assert cli.evidence(tmp_path / "staged") == (
        snapshots.digest(captured),
        snapshots.digest(target),
    )


@pytest.mark.parametrize(
    "changed", ["source", "target", "incomplete", "reuse", "code", "symlink", "duplicate"]
)
def test_bad_staging_evidence_is_rejected(tmp_path, changed):
    folder = tmp_path / "evidence"
    source, target, receipt = staging(folder)
    if changed == "source":
        source["unexpected"] = True
        (folder / "source-manifest.json").write_text(json.dumps(source))
    elif changed == "target":
        target["unexpected"] = True
        (folder / "target-plan.json").write_text(json.dumps(target))
    elif changed == "incomplete":
        receipt["state"] = "failed"
        (folder / "staged.json").write_text(json.dumps(receipt))
    elif changed in ("reuse", "code"):
        if changed == "reuse":
            target["containers"] = source["plan"]["containers"]
        else:
            target["source_commit"] = "b" * 40
        receipt["target_plan_sha256"] = admin.checksum(target)
        (folder / "target-plan.json").write_text(json.dumps(target))
        (folder / "staged.json").write_text(json.dumps(receipt))
    elif changed == "symlink":
        (folder / "staged.json").rename(tmp_path / "external.json")
        (folder / "staged.json").symlink_to(tmp_path / "external.json")
    else:
        (folder / "staged.json").write_text('{"format":1,"format":2}')
    with pytest.raises(admin.RestoreError):
        cli.evidence(folder)


def test_credentials_require_explicit_private_postgresql_file(tmp_path):
    path = tmp_path / "database-url"
    path.write_text("postgresql+psycopg://fixture:synthetic@127.0.0.1/collaboration_test\n")
    path.chmod(0o600)
    assert cli.url_from_file(path).database == "collaboration_test"
    path.chmod(0o644)
    with pytest.raises(admin.RestoreError, match="private"):
        cli.url_from_file(path)
    path.chmod(0o600)
    path.write_text("sqlite:///wrong.db")
    with pytest.raises(admin.RestoreError, match="PostgreSQL"):
        cli.url_from_file(path)


def test_schema_read_requires_exact_local_migration_head(db_session):
    db_session.execute(text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)"))
    db_session.execute(text("INSERT INTO alembic_version VALUES ('202611080001')"))
    assert cli.versions(db_session) == ("202611080001",)
    db_session.execute(text("UPDATE alembic_version SET version_num = '202611070005'"))
    with pytest.raises(admin.RestoreError, match="schema"):
        cli.versions(db_session)


def test_cli_does_not_use_environment_credentials_or_disclose_parse_errors(
    tmp_path, monkeypatch, capsys
):
    secret = "synthetic-never-print-this"
    path = tmp_path / "database-url"
    path.write_text(secret)
    path.chmod(0o600)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused-live-setting/db")
    monkeypatch.setattr(
        "sys.argv",
        [
            "restore",
            "--database-url-file",
            str(path),
            "receipt",
            "--plan",
            str(tmp_path / "unused"),
        ],
    )
    monkeypatch.setattr(
        cli, "create_engine", lambda *_a, **_kw: pytest.fail("Unexpected connection")
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert secret not in stderr and os.environ["DATABASE_URL"] not in stderr

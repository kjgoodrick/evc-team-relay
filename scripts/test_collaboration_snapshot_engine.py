#!/usr/bin/env python3
"""Exercise snapshot capture against newly created disposable engine fixtures.

This checks container/copy/exit mechanics and optionally real PostgreSQL/MinIO
reopening. It does not exercise application-level restore or Relay semantics.
All containers and volumes created by this script are removed in finally.
"""

import argparse
import json
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import ClassVar

import collaboration_snapshot as snapshot


def run_bytes(binary, args, body=None):
    result = subprocess.run(
        [binary, *args], input=body, capture_output=True, timeout=90, check=False
    )
    if result.returncode:
        raise RuntimeError("Disposable engine fixture command failed")
    return result.stdout


def run(binary, args):
    return run_bytes(binary, args).decode().strip()


def exercise(binary, image, failed=False, stores=None):
    engine = snapshot.Engine(binary, timeout=30)
    scope = str(uuid.uuid4())
    created = []
    volumes = []
    source_ids = {}
    with tempfile.TemporaryDirectory(prefix="evc-cold-snapshot-proof-") as temp:
        root = Path(temp)
        try:
            for role, path in snapshot.PATHS.items():
                volume = f"evc-snapshot-{scope}-{role}"
                run(binary, ["volume", "create", volume])
                volumes.append(volume)
                completion = (
                    f'echo "{snapshot.COMPLETION}"; echo "Server shut down."; '
                    if role == "relay" and not failed
                    else ""
                )
                # Every fixture writes its last bytes only during TERM. The
                # archive must contain these bytes, not the initial state.
                body = (
                    f"echo before > {path}/state; "
                    f"trap 'echo drained > {path}/state; {completion}exit "
                    f"{1 if role == 'relay' and failed else 0}' TERM; "
                    "echo fixture-ready; while :; do sleep 1 & wait $!; done"
                )
                cid = run(
                    binary,
                    [
                        "run",
                        "-d",
                        "--pull=never",
                        "--network=none",
                        "--restart=no",
                        "--name",
                        f"evc-snapshot-{scope}-{role}",
                        "--label",
                        f"{snapshot.SET_LABEL}={scope}",
                        "--label",
                        f"{snapshot.ROLE_LABEL}={role}",
                        "--mount",
                        f"type=volume,source={volume},destination={path}",
                        *(
                            stores.runtime(role)
                            if stores and role in stores.roles
                            else ["--entrypoint", "/bin/sh", image, "-c", body]
                        ),
                    ],
                )
                created.append(cid)
                source_ids[role] = cid
            for role, cid in source_ids.items():
                if stores and role in stores.roles:
                    stores.ready(cid, role)
                    continue
                deadline = time.monotonic() + 10
                while "fixture-ready" not in run(binary, ["logs", cid]):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Disposable container did not become ready")
                    time.sleep(0.05)
            if stores:
                stores.seed(source_ids)
            plan = snapshot.prepare(engine, scope, "a" * 40)
            target = root / "capture"
            if failed:
                try:
                    snapshot.capture(engine, plan, target)
                except snapshot.SnapshotError:
                    assert not (target / "complete.json").exists()
                    assert not list(target.glob("*.tar"))
                    return {"failed_relay_rejected": True, "sealed": False}
                raise AssertionError("A failed relay exit was accepted")
            manifest = snapshot.capture(engine, plan, target)
            assert snapshot.verify(target) == manifest
            for role in snapshot.PATHS:
                if stores and role in stores.roles:
                    continue
                with tarfile.open(target / f"{role}.tar") as archive:
                    entries = [entry for entry in archive if Path(entry.name).name == "state"]
                    assert len(entries) == 1
                    assert archive.extractfile(entries[0]).read() == b"drained\n"
            restored_scope = str(uuid.uuid4())
            restored_ids = []
            restored_by_role = {}
            for role, path in snapshot.PATHS.items():
                volume = f"evc-snapshot-{restored_scope}-{role}"
                run(binary, ["volume", "create", volume])
                volumes.append(volume)
                cid = run(
                    binary,
                    [
                        "create",
                        "--pull=never",
                        "--network=none",
                        "--restart=no",
                        "--name",
                        f"evc-snapshot-{restored_scope}-{role}",
                        "--label",
                        f"{snapshot.SET_LABEL}={restored_scope}",
                        "--label",
                        f"{snapshot.ROLE_LABEL}={role}",
                        "--mount",
                        f"type=volume,source={volume},destination={path}",
                        *(
                            stores.runtime(role)
                            if stores and role in stores.roles
                            else ["--entrypoint", "/bin/sh", image, "-c", "exit 0"]
                        ),
                    ],
                )
                created.append(cid)
                restored_ids.append(cid)
                restored_by_role[role] = cid
            target_plan = snapshot.prepare(engine, restored_scope, "a" * 40, fresh=True)
            receipt = snapshot.stage(engine, target, target_plan, root / "staging")
            assert (root / "staging/staged.json").is_file()
            assert all(snapshot.fresh_isolated(engine.inspect(cid)) for cid in restored_ids)
            # A second attempt must not overwrite the now populated stores.
            try:
                snapshot.stage(engine, target, target_plan, root / "second-attempt")
            except snapshot.SnapshotError as exc:
                assert "not empty" in str(exc)
                assert not (root / "second-attempt/staged.json").exists()
            else:
                raise AssertionError("Populated restore stores were overwritten")
            if stores:
                stores.reopen_and_verify(restored_by_role)
            return {
                "real_postgres_minio": stores is not None,
                "stores": len(manifest["archives"]),
                "verified": True,
                "final_bytes_captured": True,
                "isolated_staging_roundtrip": True,
                "nonempty_target_rejected": True,
                "stage_seconds": receipt["duration_seconds"],
                "seconds": manifest["duration_seconds"],
            }
        finally:
            for cid in reversed(created):
                run(binary, ["rm", "-f", cid])
            for volume in reversed(volumes):
                run(binary, ["volume", "rm", volume])


class RealStores:
    """Only synthetic bytes, fresh volumes, and container-local connections."""

    roles = frozenset(("postgres", "objects"))
    payloads: ClassVar[dict[str, bytes]] = {
        "comment-pictures/v1/proof/original": b"private-comment-picture\x00\xff",
        "managed-assets/v1/proof/image": b"managed-image-revision\x00\xfe",
        "web-assets/proof/legacy": b"legacy-object\x00\xfd",
        "relay-document/proof/data.ysweet": b"opaque-checkpoint\x00\xfc",
    }

    def __init__(self, binary, postgres_image, minio_image):
        self.binary = binary
        self.postgres_image = postgres_image
        self.minio_image = minio_image

    def runtime(self, role):
        if role == "postgres":
            return [
                "--env",
                "POSTGRES_PASSWORD=disposable-snapshot-password",
                "--env",
                "POSTGRES_USER=fixture",
                "--env",
                "POSTGRES_DB=fixture",
                "--env",
                "POSTGRES_INITDB_ARGS=--data-checksums",
                self.postgres_image,
            ]
        return [
            "--env",
            "MINIO_ROOT_USER=snapshot-fixture",
            "--env",
            "MINIO_ROOT_PASSWORD=disposable-snapshot-password",
            self.minio_image,
            "server",
            "/data",
        ]

    def ready(self, cid, role):
        probe = (
            ["pg_isready", "-U", "fixture", "-d", "fixture"]
            if role == "postgres"
            else ["curl", "-fsS", "http://127.0.0.1:9000/minio/health/ready"]
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                run(self.binary, ["exec", cid, *probe])
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable real store did not become ready") from None
                time.sleep(0.1)

    def sql(self, cid, statement):
        return run(
            self.binary,
            [
                "exec",
                cid,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "fixture",
                "-d",
                "fixture",
                "-At",
                "-c",
                statement,
            ],
        )

    def object(self, cid, method, path, body=None):
        args = [
            "exec",
            "-i",
            cid,
            "curl",
            "-fsS",
            "--aws-sigv4",
            "aws:amz:us-east-1:s3",
            "--user",
            "snapshot-fixture:disposable-snapshot-password",
            "-X",
            method,
            f"http://127.0.0.1:9000/snapshot-proof{path}",
        ]
        if body is not None:
            args += ["--data-binary", "@-"]
        return run_bytes(self.binary, args, body)

    def seed(self, ids):
        self.sql(
            ids["postgres"],
            "CREATE TABLE snapshot_probe (object_key text PRIMARY KEY, content bytea NOT NULL)",
        )
        self.object(ids["objects"], "PUT", "")
        for key, body in self.payloads.items():
            self.object(ids["objects"], "PUT", "/" + key, body)
            self.sql(
                ids["postgres"],
                f"INSERT INTO snapshot_probe VALUES ('{key}', decode('{body.hex()}', 'hex'))",
            )

    def reopen_and_verify(self, ids):
        for role in ("postgres", "objects"):
            run(self.binary, ["start", ids[role]])
            self.ready(ids[role], role)
        actual = self.sql(
            ids["postgres"],
            "SELECT object_key || '|' || encode(content, 'hex') "
            "FROM snapshot_probe ORDER BY object_key",
        )
        assert actual == "\n".join(
            f"{key}|{body.hex()}" for key, body in sorted(self.payloads.items())
        )
        for key, body in self.payloads.items():
            assert self.object(ids["objects"], "GET", "/" + key) == body
        # Ordinary write/read works in the isolated restored services too.
        self.sql(
            ids["postgres"],
            "INSERT INTO snapshot_probe VALUES ('after-restore', decode('1234', 'hex'))",
        )
        self.object(ids["objects"], "PUT", "/after-restore", b"new bytes")
        assert self.object(ids["objects"], "GET", "/after-restore") == b"new bytes"
        for role in ("postgres", "objects"):
            snapshot.Engine(self.binary).stop(ids[role])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument(
        "--image", required=True, help="Already cached image with /bin/sh; never pulled"
    )
    parser.add_argument(
        "--postgres-image",
        help="Cached PostgreSQL image for an additional real-store roundtrip",
    )
    parser.add_argument(
        "--minio-image",
        help="Cached MinIO image with curl for the real-store roundtrip",
    )
    parser.add_argument(
        "--stores-only",
        action="store_true",
        help="Run just the additional real-store case",
    )
    args = parser.parse_args()
    if bool(args.postgres_image) != bool(args.minio_image) or (
        args.stores_only and not args.postgres_image
    ):
        parser.error("Both real-store images are required together")
    engine = snapshot.Engine(args.engine)
    before = {cid: engine.inspect(cid)["State"] for cid in engine.ids()}
    results = (
        []
        if args.stores_only
        else [exercise(args.engine, args.image, failed) for failed in (False, True)]
    )
    if args.postgres_image:
        results.append(
            exercise(
                args.engine,
                args.image,
                stores=RealStores(args.engine, args.postgres_image, args.minio_image),
            )
        )
    after = {cid: engine.inspect(cid)["State"] for cid in engine.ids()}
    assert set(before) == set(after)
    for cid, state in before.items():
        assert state["Running"] == after[cid]["Running"]
        assert state["StartedAt"] == after[cid]["StartedAt"]
    print(
        json.dumps(
            {"passed": True, "existing_containers_unchanged": True, "results": results},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

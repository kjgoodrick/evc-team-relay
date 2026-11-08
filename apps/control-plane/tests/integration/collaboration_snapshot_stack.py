"""Cold restore/rollback proof using real application images and the plugin client.

Run outside the process sandbox when using Podman. All services, volumes,
networks, credentials and client databases belong to this disposable fixture.
The script never starts, stops or removes an existing service. Evidence stays
in a newly created private --output directory; owned runtime resources are
removed in finally. This is a rehearsal, not production activation tooling.
"""

import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import cbor2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CP = Path(__file__).resolve().parents[2]
SERVER = CP.parents[1]
sys.path.insert(0, str(CP))
sys.path.insert(0, str(SERVER / "scripts"))

import collaboration_snapshot as snapshot  # noqa: E402


def run(argv, *, env=None, cwd=CP, timeout=120):
    result = subprocess.run(argv, capture_output=True, cwd=cwd, env=env, timeout=timeout)
    if result.returncode:
        # Commands can include synthetic credentials. Keep errors in private
        # fixture logs; do not dump command arguments into shared output.
        raise RuntimeError(
            f"Fixture command failed ({Path(argv[0]).name}): " + result.stderr.decode()[-1500:]
        )
    return result.stdout.decode().strip()


def wait(check, label, seconds=60):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out: " + label)
        time.sleep(0.1)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def configure(folder, cp_port):
    from app.core import security

    private, public = security.generate_ed25519_keypair()
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    protected = cbor2.dumps({1: -8, 4: b"cold-fixture"})
    claims = cbor2.dumps(
        {
            1: "relay-server",
            3: "http://relay:8080",
            4: int(time.time()) + 3600,
            6: int(time.time()),
            -80201: "server",
        }
    )
    signature = key.sign(cbor2.dumps(["Signature1", protected, b"", claims]))
    token = (
        base64.urlsafe_b64encode(
            cbor2.dumps(cbor2.CBORTag(61, cbor2.CBORTag(18, [protected, {}, claims, signature])))
        )
        .decode()
        .rstrip("=")
    )
    config = folder / "relay.toml"
    config.write_text(f'''[server]
host = "0.0.0.0"
url = "http://relay:8080"
port = 8080
checkpoint_freq_seconds = 1
collaboration_anchor_barriers = true
[[auth]]
key_id = "managed-test"
public_key = "{public}"
[[auth]]
key_id = "cold-fixture"
public_key = "{base64.b64encode(raw).decode()}"
allowed_token_types = ["server"]
[store]
type = "filesystem"
path = "/app/data"
''')
    # Public verification keys only; the unprivileged relay must read this mount.
    config.chmod(0o644)
    settings = {
        "DATABASE_URL": "postgresql+psycopg://fixture:disposable-cold-password@postgres:5432/collaboration_test",
        "JWT_SECRET": secrets.token_hex(32),
        "RELAY_PRIVATE_KEY": base64.b64encode(private.encode()).decode(),
        "RELAY_KEY_ID": "managed-test",
        "RELAY_AUDIENCE": "http://relay:8080",
        "RELAY_PUBLIC_URL": f"ws://127.0.0.1:{cp_port}",
        "MANAGED_RELAY_URL": "ws://relay:8080",
        "CONTROL_PLANE_PUBLIC_URL": f"http://127.0.0.1:{cp_port}",
        "COLLABORATION_ANCHOR_RELAY_TOKEN": token,
        "COLLABORATION_POLICY_ENABLED": "true",
        "COLLABORATION_DISCUSSIONS_ENABLED": "true",
        "COLLABORATION_TEXT_ANCHORS_ENABLED": "true",
        "COLLABORATION_PICTURES_ENABLED": "true",
        "COLLABORATION_INBOX_ENABLED": "true",
        "COLLABORATION_PICTURES_BUCKET": "cold-fixture-pictures",
        "MINIO_ENDPOINT": "objects:9000",
        "MINIO_ACCESS_KEY": "cold-fixture",
        "MINIO_SECRET_KEY": "disposable-cold-password",
        "MINIO_SECURE": "false",
        "LOG_LEVEL": "INFO",
    }
    envfile = folder / "application.env"
    envfile.write_text("".join(f"{name}={value}\n" for name, value in settings.items()))
    envfile.chmod(0o600)
    return settings


class Stack:
    def __init__(self, args, folder, settings):
        self.args, self.folder, self.settings = args, folder, settings
        self.engine = snapshot.Engine(args.engine, timeout=180)
        self.containers, self.volumes, self.networks = [], [], []
        self.cp_port = int(settings["CONTROL_PLANE_PUBLIC_URL"].rsplit(":", 1)[1])
        self.images = {
            role: self.call("image", "inspect", image, "--format", "{{.Id}}")
            for role, image in {
                "postgres": args.postgres_image,
                "objects": args.minio_image,
                "control-plane": args.control_plane_image,
                "relay": args.relay_image,
            }.items()
        }

    def call(self, *args):
        return run([self.args.engine, *args])

    def group(self, fresh=False, volumes=None):
        group = {"set": str(uuid.uuid4()), "ids": {}, "volumes": volumes or {}}
        network = "none"
        if not fresh:
            network = "evc-cold-" + group["set"]
            self.call("network", "create", "--internal", network)
            self.networks.append(network)
        for role, mount in snapshot.PATHS.items():
            if role not in group["volumes"]:
                volume = "evc-cold-" + group["set"] + "-" + role
                self.call("volume", "create", volume)
                self.volumes.append(volume)
                group["volumes"][role] = volume
            options = [
                "create",
                "--pull=never",
                "--restart=no",
                "--network",
                network,
                "--name",
                "evc-cold-" + group["set"] + "-" + role,
                "--label",
                f"{snapshot.SET_LABEL}={group['set']}",
                "--label",
                f"{snapshot.ROLE_LABEL}={role}",
                "-v",
                f"{group['volumes'][role]}:{mount}" + ("" if volumes else ":U"),
            ]
            if not fresh:
                options += ["--network-alias", role]
            if role == "postgres":
                if not fresh:
                    options += ["-p", "127.0.0.1::5432"]
                options += [
                    "-e",
                    "POSTGRES_USER=fixture",
                    "-e",
                    "POSTGRES_PASSWORD=disposable-cold-password",
                    "-e",
                    "POSTGRES_DB=collaboration_test",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--data-checksums",
                    self.images[role],
                ]
            elif role == "objects":
                if not fresh:
                    options += ["-p", "127.0.0.1::9000"]
                options += [
                    "-e",
                    "MINIO_ROOT_USER=cold-fixture",
                    "-e",
                    "MINIO_ROOT_PASSWORD=disposable-cold-password",
                    self.images[role],
                    "server",
                    "/data",
                ]
            elif role == "relay":
                options += [
                    self.images[role],
                    "serve",
                    "--config",
                    "/app/relay.toml",
                ]
            else:
                if not fresh:
                    options += ["-p", f"127.0.0.1:{self.cp_port}:8000"]
                options += [
                    "--init",
                    "--env-file",
                    str(self.folder / "application.env"),
                    "--entrypoint",
                    "uvicorn",
                    self.images[role],
                    "app.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--no-access-log",
                    "--lifespan",
                    "on",
                ]
            cid = self.call(*options)
            self.containers.append(cid)
            group["ids"][role] = cid
            if role == "relay":
                # The remote Podman VM need not share host filesystem paths.
                # Reprovision this exact public-key config for every fixture
                # runtime, outside the captured /app/data store. This is not
                # a deployment/configuration-backup implementation.
                self.call("cp", str(self.folder / "relay.toml"), cid + ":/app/relay.toml")
        return group

    def start(self, group, role):
        cid = group["ids"][role]
        self.call("start", cid)
        if role in ("postgres", "objects"):
            probe = (
                ["pg_isready", "-U", "fixture", "-d", "collaboration_test"]
                if role == "postgres"
                else ["curl", "-fsS", "http://127.0.0.1:9000/minio/health/ready"]
            )
            wait(
                lambda: (
                    subprocess.run(
                        [self.args.engine, "exec", cid, *probe], capture_output=True
                    ).returncode
                    == 0
                ),
                role,
            )
        elif role == "control-plane":

            def ready():
                state = self.engine.inspect(cid)["State"]
                if not state["Running"]:
                    raise RuntimeError("Disposable control plane exited during startup")
                return b"Application startup complete." in self.engine.logs(cid, state["StartedAt"])

            wait(ready, "control plane startup")

    def database(self, group):
        mapping = self.call("port", group["ids"]["postgres"], "5432/tcp")
        assert mapping.startswith("127.0.0.1:") and "\n" not in mapping
        return f"postgresql+psycopg://fixture:disposable-cold-password@{mapping}/collaboration_test"

    def restore(self, source, number):
        target = self.group(fresh=True)
        target_plan = snapshot.prepare(
            self.engine, target["set"], self.args.source_commit, fresh=True
        )
        evidence = self.folder / f"stage-{number}"
        snapshot.stage(self.engine, source, target_plan, evidence)
        assert all(
            snapshot.fresh_isolated(self.engine.inspect(cid)) for cid in target["ids"].values()
        )
        # Runtime network changes are a fixture-only transition. Preserve the
        # staged named volumes and record both identities; never reopen sources.
        for cid in target["ids"].values():
            self.call("rm", cid)
            self.containers.remove(cid)
        runtime = self.group(volumes=target["volumes"])
        snapshot.private_json(
            self.folder / f"transition-{number}.json", {"staged": target, "runtime": runtime}
        )
        self.start(runtime, "postgres")
        credential = self.folder / f"database-{number}"
        credential.write_text(self.database(runtime))
        credential.chmod(0o600)
        cli = [
            sys.executable,
            "-m",
            "app.maintenance.collaboration_restore",
            "--database-url-file",
            str(credential),
        ]
        plan = self.folder / f"rotation-{number}.json"
        run(
            [
                *cli,
                "plan",
                "--staging-evidence",
                str(evidence),
                "--operator",
                "Disposable full stack restore",
                "--output",
                str(plan),
            ]
        )
        run(
            [
                *cli,
                "apply",
                "--staging-evidence",
                str(evidence),
                "--plan",
                str(plan),
                "--output",
                str(self.folder / f"rotation-result-{number}.json"),
            ]
        )
        assert json.loads(run([*cli, "receipt", "--plan", str(plan)]))["state"] == "recorded"
        for role in ("objects", "relay", "control-plane"):
            self.start(runtime, role)
        return runtime, json.loads(plan.read_text())["generation"]

    def cleanup(self):
        failures = []
        for cid in reversed(self.containers):
            # Force is restricted to resources created and recorded by this run.
            try:
                result = subprocess.run(
                    [self.args.engine, "logs", cid], capture_output=True, timeout=30
                )
                (self.folder / (cid + ".log")).write_bytes(result.stdout + result.stderr)
            except Exception:
                failures.append(cid + " (log capture)")
            try:
                self.call("rm", "-f", cid)
            except Exception:
                failures.append(cid)
        for kind, names in (("volume", self.volumes), ("network", self.networks)):
            for name in reversed(names):
                try:
                    self.call(kind, "rm", name)
                except Exception:
                    failures.append(name)
        if failures:
            raise RuntimeError("Disposable cleanup failed: " + ", ".join(failures))


def seed(stack, group):
    from minio import Minio
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    os.environ.update(stack.settings, DATABASE_URL=stack.database(group))
    from app.core import security
    from app.core.config import get_settings
    from app.db import models as m
    from app.services import managed_service, session_service

    get_settings.cache_clear()
    run([sys.executable, "-m", "alembic", "upgrade", "head"], env=dict(os.environ))
    engine = create_engine(stack.database(group), poolclass=NullPool)
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@cold.fixture", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@cold.fixture", password_hash="", is_active=True)
        db.add_all([owner, reviewer])
        db.flush()
        share = m.Share(
            kind=m.ShareKind.FOLDER,
            path="Team",
            managed=True,
            visibility=m.ShareVisibility.PRIVATE,
            owner_user_id=owner.id,
        )
        db.add(share)
        db.flush()
        db.add_all(
            [
                m.ShareMember(
                    share_id=share.id, user_id=reviewer.id, role=m.ShareMemberRole.VIEWER
                ),
                m.ManagedRule(
                    share_id=share.id,
                    user_id=reviewer.id,
                    path="",
                    recursive=True,
                    permission="read",
                ),
                m.ManagedRule(
                    share_id=share.id,
                    user_id=reviewer.id,
                    path="Budget.md",
                    recursive=False,
                    permission="none",
                ),
                m.ManagedCommentRule(
                    share_id=share.id, user_id=reviewer.id, path="", recursive=True, allowed=True
                ),
            ]
        )
        db.commit()
        note, hidden = str(uuid.uuid4()), str(uuid.uuid4())
        managed_service.apply_batch(
            db,
            share,
            owner,
            managed_service.Batch(
                changes=[
                    managed_service.Change(
                        path=path,
                        previous=None,
                        value=managed_service.Record(id=entry, type="markdown"),
                    )
                    for path, entry in [("Note.md", note), ("Budget.md", hidden)]
                ]
            ),
        )
        sessions = [session_service.create_session(db, user.id)[0] for user in (owner, reviewer)]
        db.commit()
        fixture = {
            "base": stack.settings["CONTROL_PLANE_PUBLIC_URL"],
            "share": str(share.id),
            "note": note,
            "hidden": hidden,
            "ownerId": str(owner.id),
            "reviewerId": str(reviewer.id),
            "ownerToken": security.create_access_token(
                str(owner.id), session_id=str(sessions[0].id)
            ),
            "reviewerToken": security.create_access_token(
                str(reviewer.id), session_id=str(sessions[1].id)
            ),
            "restartDir": str(stack.folder),
            "picturePath": str(stack.folder / "picture.png"),
        }
    engine.dispose()
    objects = stack.call("port", group["ids"]["objects"], "9000/tcp")
    assert objects.startswith("127.0.0.1:")
    Minio(
        objects, access_key="cold-fixture", secret_key="disposable-cold-password", secure=False
    ).make_bucket("cold-fixture-pictures")
    with Image.new("RGBA", (800, 600), (40, 80, 120, 160)) as image:
        image.save(fixture["picturePath"], format="PNG")
    snapshot.private_json(stack.folder / "fixture.json", fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["podman", "docker"], required=True)
    for name in (
        "postgres-image",
        "minio-image",
        "control-plane-image",
        "relay-image",
        "source-commit",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert snapshot.COMMIT.fullmatch(args.source_commit)
    assert run(["git", "rev-parse", args.source_commit], cwd=SERVER) == args.source_commit
    assert not run(
        [
            "git",
            "diff",
            args.source_commit,
            "--",
            "apps/control-plane/app",
            "scripts/collaboration_snapshot.py",
        ],
        cwd=SERVER,
    )
    args.output.mkdir(mode=0o700)
    os.umask(0o077)
    settings = configure(args.output, free_port())
    stack = Stack(args, args.output, settings)
    before = {
        cid: stack.engine.inspect(cid)["State"]
        for cid in stack.call("ps", "-aq", "--no-trunc").splitlines()
    }
    client = None
    try:
        source = stack.group()
        for role in ("postgres", "objects"):
            stack.start(source, role)
        seed(stack, source)
        for role in ("relay", "control-plane"):
            stack.start(source, role)
        with (args.output / "client.log").open("w") as log:
            client = subprocess.Popen(
                [
                    "node",
                    "node_modules/jest/bin/jest.js",
                    "--runInBand",
                    "--runTestsByPath",
                    "__tests__/collaboration/realColdRestoreStack.test.ts",
                ],
                cwd=args.plugin,
                env={**os.environ, "EVC_COLD_RESTORE_FIXTURE": str(args.output / "fixture.json")},
                stdout=log,
                stderr=log,
            )
            current = source
            generations = []
            for number in (1, 2):
                request = args.output / f"restore-{number}-request"
                wait(
                    lambda: request.exists() or client.poll() is not None,
                    "client restore request",
                    120,
                )
                assert request.exists(), "Client failed before restore; see client.log"
                plan = snapshot.prepare(stack.engine, current["set"], args.source_commit)
                capture = args.output / f"capture-{number}"
                snapshot.capture(stack.engine, plan, capture)
                print(f"Captured actual stack {number}", flush=True)
                # Both restores use the original backup. The second capture
                # preserves the post-restore state before rehearsing rollback.
                current, generation = stack.restore(args.output / "capture-1", number)
                assert generation not in generations
                generations.append(generation)
                snapshot.private_json(
                    args.output / f"restore-{number}-done", {"generation": generation}
                )
                print(f"Restored actual stack {number} with fresh generation", flush=True)
            assert client.wait(timeout=90) == 0, "Client proof failed; see client.log"
        result = {
            "passed": True,
            "source_commit": args.source_commit,
            "images": stack.images,
            "generations": generations,
            "restores": 2,
        }
    finally:
        if client is not None and client.poll() is None:
            client.terminate()
            client.wait(timeout=10)
        stack.cleanup()
        after = stack.call("ps", "-aq", "--no-trunc").splitlines()
        assert set(before) == set(after), "Existing container inventory changed"
        for cid, state in before.items():
            observed = stack.engine.inspect(cid)["State"]
            assert all(
                observed[key] == state[key] for key in ("Running", "StartedAt", "FinishedAt")
            )
    snapshot.private_json(args.output / "result.json", result)
    print(
        "Full application cold restore and rollback passed; disposable services removed", flush=True
    )


if __name__ == "__main__":
    main()

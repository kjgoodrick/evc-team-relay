"""Run the plugin's real-socket tests against disposable FastAPI + Rust servers.

Requires the relay binary built with cargo and the plugin's npm dependencies.
No existing services, vaults, credentials or containers are used.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ready(number, process):
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError("Disposable server exited; inspect the temporary test log")
        try:
            with socket.create_connection(("127.0.0.1", number), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Disposable server did not start")


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument(
        "--relay", type=Path, default=ROOT.parent / "relay-server/target/debug/relay"
    )
    parser.add_argument(
        "--serve-seconds",
        type=int,
        default=0,
        help="Hold a disposable desktop smoke-test server open instead of running Jest",
    )
    args = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="evc-managed-integration-") as temporary:
        temp = Path(temporary)
        cp_port, relay_port = port(), port()
        os.environ.update(
            {
                "DATABASE_URL": "sqlite+pysqlite:///" + str(temp / "test.db"),
                "JWT_SECRET": secrets.token_hex(32),
                "LOG_LEVEL": "WARNING",
                "CONTROL_PLANE_PUBLIC_URL": f"http://127.0.0.1:{cp_port}",
                "RELAY_PUBLIC_URL": f"ws://127.0.0.1:{relay_port}",
                "MANAGED_RELAY_URL": f"ws://127.0.0.1:{relay_port}",
                "RELAY_AUDIENCE": f"http://127.0.0.1:{relay_port}",
                "RELAY_KEY_ID": "managed-test",
                "BOOTSTRAP_ADMIN_EMAIL": "owner@example.com",
                "BOOTSTRAP_ADMIN_PASSWORD": secrets.token_hex(24),
            }
        )
        from app.core import security

        private, public = security.generate_ed25519_keypair()
        os.environ["RELAY_PRIVATE_KEY"] = private
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.db import models, session
        from app.services import managed_service, session_service

        models.Base.metadata.create_all(session.get_engine())
        with session.get_sessionmaker()() as db:
            owner = models.User(
                email="owner@example.com",
                password_hash=security.get_password_hash("disposable-preview-password"),
                is_admin=True,
                is_active=True,
            )
            bob = models.User(
                email="bob@example.com", password_hash="", is_admin=False, is_active=True
            )
            db.add_all([owner, bob])
            db.flush()
            share = models.Share(
                kind=models.ShareKind.FOLDER,
                path="Team",
                visibility=models.ShareVisibility.PRIVATE,
                managed=True,
                owner_user_id=owner.id,
            )
            db.add(share)
            db.flush()
            db.add(
                models.ShareMember(
                    share_id=share.id, user_id=bob.id, role=models.ShareMemberRole.EDITOR
                )
            )
            db.add(
                models.ManagedRule(
                    share_id=share.id, user_id=bob.id, path="", recursive=True, permission="write"
                )
            )
            db.add(
                models.ManagedRule(
                    share_id=share.id,
                    user_id=bob.id,
                    path="Budget.md",
                    recursive=False,
                    permission="none",
                )
            )
            db.commit()
            import uuid

            note, secret = str(uuid.uuid4()), str(uuid.uuid4())
            managed_service.apply_batch(
                db,
                share,
                owner,
                managed_service.Batch(
                    changes=[
                        managed_service.Change(
                            path=path,
                            previous=None,
                            value=managed_service.Record(id=id, type="markdown"),
                        )
                        for path, id in [("Note.md", note), ("Budget.md", secret)]
                    ]
                ),
            )
            owner_session, _ = session_service.create_session(db, owner.id)
            bob_session, _ = session_service.create_session(db, bob.id)
            db.commit()
            fixture = {
                "base": f"http://127.0.0.1:{cp_port}",
                "relay": f"ws://127.0.0.1:{relay_port}",
                "share": str(share.id),
                "note": note,
                "secret": secret,
                "bobId": str(bob.id),
                "ownerToken": security.create_access_token(
                    str(owner.id), session_id=str(owner_session.id)
                ),
                "bobToken": security.create_access_token(
                    str(bob.id), session_id=str(bob_session.id)
                ),
                "restartDir": str(temp),
            }
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fixture_path.chmod(0o600)
        config = temp / "relay.toml"
        config.write_text(f'''[server]
host = "127.0.0.1"
url = "http://127.0.0.1:{relay_port}"
port = {relay_port}
checkpoint_freq_seconds = 1
[[auth]]
key_id = "managed-test"
public_key = "{public}"
[store]
type = "filesystem"
path = "{temp / "relay-data"}"
''')
        relay_cmd = [str(args.relay.resolve()), "serve", "--config", str(config)]
        try:
            with (temp / "servers.log").open("w") as log:
                relay = subprocess.Popen(relay_cmd, cwd=temp, stdout=log, stderr=log)
                processes.append(relay)
                ready(relay_port, relay)
                cp = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(cp_port),
                        "--no-access-log",
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                processes.append(cp)
                ready(cp_port, cp)
                if args.serve_seconds:
                    print(f"Disposable server: http://127.0.0.1:{cp_port}", flush=True)
                    print("Test login: owner@example.com / disposable-preview-password", flush=True)
                    print(f"Fixture: {fixture_path}", flush=True)
                    deadline = time.monotonic() + args.serve_seconds
                    while time.monotonic() < deadline:
                        if cp.poll() is not None or relay.poll() is not None:
                            raise RuntimeError("Disposable server exited")
                        time.sleep(0.2)
                    return 0
                env = {**os.environ, "EVC_MANAGED_FIXTURE": str(fixture_path)}
                test = subprocess.Popen(
                    ["npm", "test", "--", "--runInBand", "__tests__/managed/realStack.test.ts"],
                    cwd=args.plugin,
                    env=env,
                )
                processes.append(test)
                while test.poll() is None:
                    if (temp / "restart-request").exists():
                        (temp / "restart-request").unlink()
                        stop(relay)
                        relay = subprocess.Popen(relay_cmd, cwd=temp, stdout=log, stderr=log)
                        processes.append(relay)
                        ready(relay_port, relay)
                        (temp / "restart-done").write_text("ready")
                    time.sleep(0.05)
                if test.returncode:
                    # Server logs contain only disposable data but may contain
                    # credentials; retain locally on failure, never print them.
                    import shutil

                    destination = Path(tempfile.mkdtemp(prefix="evc-managed-failure-"))
                    shutil.copy(temp / "servers.log", destination / "servers.log")
                    print(f"Local diagnostic log: {destination / 'servers.log'}")
                return test.returncode
        except Exception:
            import shutil

            destination = Path(tempfile.mkdtemp(prefix="evc-managed-failure-"))
            if (temp / "servers.log").exists():
                shutil.copy(temp / "servers.log", destination / "servers.log")
            print(f"Local diagnostic log: {destination / 'servers.log'}")
            raise
        finally:
            for process in reversed(processes):
                stop(process)


if __name__ == "__main__":
    raise SystemExit(main())

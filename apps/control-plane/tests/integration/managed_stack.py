"""Run the plugin's real-socket tests against disposable FastAPI + Rust servers.

Requires the relay binary built with cargo and the plugin's npm dependencies.
No existing services, vaults, credentials or containers are used.
"""

from __future__ import annotations

import argparse
import base64
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
    parser.add_argument("--presence", action="store_true", help="Run authenticated awareness tests")
    parser.add_argument(
        "--presence-locations", action="store_true", help="Run cross-note location transport tests"
    )
    parser.add_argument(
        "--text-anchors",
        action="store_true",
        help="Run selected-text comments through the private relay barrier",
    )
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
        private_auth = ""
        if args.text_anchors:
            # A separate, disposable server-only signing key. The application's
            # normal document key must never authorize the private barrier.
            import cbor2
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            barrier_key = Ed25519PrivateKey.generate()
            barrier_public = base64.b64encode(
                barrier_key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode()
            protected = cbor2.dumps({1: -8, 4: b"anchor-fixture"})
            claims = cbor2.dumps(
                {
                    1: "relay-server",
                    3: f"http://127.0.0.1:{relay_port}",
                    4: int(time.time()) + max(300, args.serve_seconds + 300),
                    6: int(time.time()),
                    -80201: "server",
                }
            )
            signature = barrier_key.sign(cbor2.dumps(["Signature1", protected, b"", claims]))
            token = (
                base64.urlsafe_b64encode(
                    cbor2.dumps(
                        cbor2.CBORTag(61, cbor2.CBORTag(18, [protected, {}, claims, signature]))
                    )
                )
                .decode()
                .rstrip("=")
            )
            os.environ.update(
                COLLABORATION_POLICY_ENABLED="true",
                COLLABORATION_DISCUSSIONS_ENABLED="true",
                COLLABORATION_TEXT_ANCHORS_ENABLED="true",
                COLLABORATION_ANCHOR_RELAY_TOKEN=token,
            )
            private_auth = f'''[[auth]]
key_id = "anchor-fixture"
public_key = "{barrier_public}"
allowed_token_types = ["server"]
'''
        from app.core.config import get_settings

        if args.presence_locations:
            os.environ["COLLABORATION_PRESENCE_ENABLED"] = "true"

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
            if args.presence:
                provider = models.OAuthProvider(
                    name="presence-fixture",
                    issuer_url="https://identity.example.test",
                    client_id="fixture",
                    client_secret_encrypted="unused-test-only",
                )
                db.add(provider)
                db.flush()
                db.add(
                    models.UserOAuthAccount(
                        user_id=owner.id,
                        provider_id=provider.id,
                        provider_user_id="owner",
                        email=owner.email,
                        name="Fixture Owner",
                        picture_url="https://identity.example.test/owner.png",
                    )
                )
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
                    share_id=share.id,
                    user_id=bob.id,
                    path="",
                    recursive=True,
                    permission="read" if args.text_anchors else "write",
                )
            )
            if args.text_anchors:
                db.add(
                    models.ManagedCommentRule(
                        share_id=share.id, user_id=bob.id, path="", recursive=True, allowed=True
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

            note, secret, second = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            entries = [("Note.md", note), ("Budget.md", secret)]
            if args.presence_locations:
                entries.append(("Second.md", second))
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
                        for path, id in entries
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
                "second": second if args.presence_locations else None,
                "bobId": str(bob.id),
                "ownerId": str(owner.id),
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
collaboration_anchor_barriers = {str(args.text_anchors).lower()}
[[auth]]
key_id = "managed-test"
public_key = "{public}"
{private_auth}\
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
                fixture_variable = (
                    "EVC_LOCATION_FIXTURE"
                    if args.presence_locations
                    else "EVC_PRESENCE_FIXTURE"
                    if args.presence
                    else "EVC_TEXT_ANCHOR_FIXTURE"
                    if args.text_anchors
                    else "EVC_MANAGED_FIXTURE"
                )
                test_file = (
                    "__tests__/presence/realLocationStack.test.ts"
                    if args.presence_locations
                    else "__tests__/presence/realPresenceStack.test.ts"
                    if args.presence
                    else "__tests__/collaboration/realTextAnchorStack.test.ts"
                    if args.text_anchors
                    else "__tests__/managed/realStack.test.ts"
                )
                env = {**os.environ, fixture_variable: str(fixture_path)}
                test = subprocess.Popen(
                    ["npm", "test", "--", "--runInBand", test_file],
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

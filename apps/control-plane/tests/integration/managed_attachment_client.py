"""Actual plugin attachment transport, loopback HTTP and disposable MinIO.

SQLite supplies the HTTP fixture; managed_attachment_postgres.py verifies locks.
Only a new bucket and temporary database are used. No native vault is involved.
"""

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from managed_stack import port, ready, stop

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--minio-endpoint", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{3,4}", args.minio_endpoint):
        raise RuntimeError("Use a disposable loopback MinIO endpoint")
    with tempfile.TemporaryDirectory(prefix="evc-managed-attachments-") as temporary:
        temp, number = Path(temporary), port()
        bucket = "managed-attachment-test-" + uuid.uuid4().hex
        os.environ.update(
            DATABASE_URL="sqlite+pysqlite:///" + str(temp / "test.db"),
            JWT_SECRET=secrets.token_hex(32),
            LOG_LEVEL="WARNING",
            CONTROL_PLANE_PUBLIC_URL=f"http://127.0.0.1:{number}",
            MINIO_ENDPOINT=args.minio_endpoint,
            MINIO_ACCESS_KEY="attachment-test",
            MINIO_SECRET_KEY="disposable-attachment-test-password",
            MINIO_SECURE="false",
            MINIO_BUCKET=bucket,
        )
        from app.core import security
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.clients import storage_client
        from app.db import models as m
        from app.db import session
        from app.services import managed_assets, session_service

        m.Base.metadata.create_all(session.get_engine())
        legacy = b"verified legacy attachment fixture"
        with session.get_sessionmaker()() as db:
            user = m.User(email="owner@attachments.fixture", password_hash="", is_active=True)
            db.add(user)
            db.flush()
            share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Fixture",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=user.id,
                collaboration_restore_generation=uuid.uuid4(),
            )
            db.add(share)
            db.flush()
            entry, old = str(uuid.uuid4()), str(uuid.uuid4())
            for identity, path, digest in [
                (entry, "Upload.png", None),
                (old, "Legacy.png", hashlib.sha256(legacy).hexdigest()),
            ]:
                record = {"id": identity, "type": "image", "version": 0}
                if digest:
                    record["hash"] = digest
                db.add(m.ManagedEntry(id=identity, share_id=share.id, path=path, record=record))
            login, _ = session_service.create_session(db, user.id)
            db.commit()
            fixture = dict(
                base=f"http://127.0.0.1:{number}",
                share=str(share.id),
                entry=entry,
                legacy=old,
                legacyHash=hashlib.sha256(legacy).hexdigest(),
                generation=str(share.collaboration_restore_generation),
                user=str(user.id),
                token=security.create_access_token(str(user.id), session_id=str(login.id)),
            )
            fixture["oldToken"] = security.create_file_token(
                subject=str(user.id),
                share_id=str(share.id),
                path=entry,
                sha256=hashlib.sha256(b"original credential").hexdigest(),
                content_type="image/png",
                content_length=len(b"original credential"),
                expires_minutes=10,
                session_id=str(login.id),
            )
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fixture_path.chmod(0o600)
        client, created, process = storage_client.get_minio_client(), False, None
        try:
            if client.bucket_exists(bucket):
                raise RuntimeError("Expected a new disposable bucket")
            client.make_bucket(bucket)
            created = True
            client.put_object(
                bucket,
                f"web-assets/{share.id}/{old}",
                io.BytesIO(legacy),
                len(legacy),
                content_type="image/png",
            )
            with (temp / "server.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(number),
                        "--no-access-log",
                        "--lifespan",
                        "off",
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                ready(number, process)
                subprocess.run(
                    [
                        "node",
                        str(args.plugin.resolve() / "node_modules/jest/bin/jest.js"),
                        "--runInBand",
                        "__tests__/managed/realManagedAttachments.test.ts",
                    ],
                    cwd=args.plugin.resolve(),
                    env={**os.environ, "EVC_MANAGED_ATTACHMENT_FIXTURE": str(fixture_path)},
                    check=True,
                    timeout=90,
                )
            keys = {item.object_name for item in client.list_objects(bucket, recursive=True)}
            expected = {f"web-assets/{share.id}/{old}"} | {
                managed_assets.object_name(share.id, entry, hashlib.sha256(value).hexdigest())
                for value in (b"first exact image revision", b"second exact image revision")
            }
            assert keys == expected, keys
            print(
                "PASS: real client/HTTP/MinIO kept distinct verified revisions; "
                "old credentials and corrupt uploads wrote no objects"
            )
        except Exception:
            if (temp / "server.log").exists():
                print((temp / "server.log").read_text()[-6000:])
            raise
        finally:
            stop(process)
            if created:
                for item in client.list_objects(bucket, recursive=True):
                    client.remove_object(bucket, item.object_name)
                client.remove_bucket(bucket)
            session.get_engine().dispose()
    print("Disposable HTTP process, SQLite fixture and MinIO bucket removed")


if __name__ == "__main__":
    main()

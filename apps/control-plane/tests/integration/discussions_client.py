"""Run the real plugin outbox against a disposable loopback control plane.

SQLite validates the HTTP/client contract here; discussions_postgres.py covers
database locking/migrations. No real vault, relay, credentials or service is used.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from managed_stack import port, ready, stop

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument(
        "--search",
        action="store_true",
        help="Run authorized search with the real TypeScript client",
    )
    parser.add_argument(
        "--inbox",
        action="store_true",
        help="Run inbox projection and client recovery with application lifecycle enabled",
    )
    parser.add_argument(
        "--pictures-endpoint",
        help="Disposable MinIO at 127.0.0.1:<port>, using synthetic picture-test credentials",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Verify portable discussion archives, optionally with picture storage",
    )
    args = parser.parse_args()
    if sum(bool(value) for value in (args.inbox, args.search, args.export)) > 1 or (
        args.pictures_endpoint and (args.inbox or args.search)
    ):
        parser.error("Choose one disposable integration scenario")
    if args.pictures_endpoint:
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", args.pictures_endpoint)
        if not match or not 1024 <= int(match[1]) <= 65535:
            raise RuntimeError("Picture tests require a disposable loopback MinIO endpoint")
    with tempfile.TemporaryDirectory(prefix="evc-discussion-client-") as temporary:
        temp, number = Path(temporary), port()
        os.environ.update(
            DATABASE_URL="sqlite+pysqlite:///" + str(temp / "test.db"),
            JWT_SECRET=secrets.token_hex(32),
            COLLABORATION_POLICY_ENABLED="true",
            COLLABORATION_DISCUSSIONS_ENABLED="true",
            COLLABORATION_EXPORT_ENABLED="true" if args.export else "false",
            COLLABORATION_SEARCH_ENABLED="true" if args.search else "false",
            LOG_LEVEL="WARNING",
            CONTROL_PLANE_PUBLIC_URL=f"http://127.0.0.1:{number}",
        )
        if args.pictures_endpoint:
            os.environ.update(
                COLLABORATION_PICTURES_ENABLED="true",
                COLLABORATION_PICTURES_BUCKET="discussion-pictures-client-" + uuid.uuid4().hex,
                MINIO_ENDPOINT=args.pictures_endpoint,
                MINIO_ACCESS_KEY="picture-test",
                MINIO_SECRET_KEY="disposable-picture-test-password",
                MINIO_SECURE="false",
            )
        from app.core import security
        from app.core.config import get_settings

        if args.inbox:
            private_key, _ = security.generate_ed25519_keypair()
            os.environ.update(
                COLLABORATION_INBOX_ENABLED="true",
                RELAY_PRIVATE_KEY=private_key,
                RELAY_PUBLIC_URL="ws://127.0.0.1:9",
                MINIO_ENDPOINT="127.0.0.1:9",
                MINIO_ACCESS_KEY="inbox-test",
                MINIO_SECRET_KEY="disposable-inbox-test",
                BOOTSTRAP_ADMIN_EMAIL="bootstrap@inbox.fixture",
                BOOTSTRAP_ADMIN_PASSWORD=secrets.token_hex(32),
            )

        get_settings.cache_clear()
        from app.db import models as m
        from app.db import session
        from app.services import session_service

        m.Base.metadata.create_all(session.get_engine())
        with session.get_sessionmaker()() as db:
            owner = m.User(email="owner@discussion.fixture", password_hash="", is_active=True)
            reviewer = m.User(email="reviewer@discussion.fixture", password_hash="", is_active=True)
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
            entry = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=share.id,
                path="Note.md",
                record={"type": "markdown"},
                deleted=False,
            )
            db.add_all(
                [
                    entry,
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
                    m.ManagedCommentRule(
                        share_id=share.id,
                        user_id=reviewer.id,
                        path="",
                        recursive=True,
                        allowed=True,
                    ),
                ]
            )
            owner_session, _ = session_service.create_session(db, owner.id)
            reviewer_session, _ = session_service.create_session(db, reviewer.id)
            db.commit()
            fixture = {
                "base": f"http://127.0.0.1:{number}",
                "share": str(share.id),
                "entry": entry.id,
                "reviewer": str(reviewer.id),
                "owner": str(owner.id),
                "token": security.create_access_token(
                    str(reviewer.id), session_id=str(reviewer_session.id)
                ),
                "ownerToken": security.create_access_token(
                    str(owner.id), session_id=str(owner_session.id)
                ),
            }
        if args.export:
            with session.get_sessionmaker()() as db:
                second = m.ManagedEntry(
                    id=str(uuid.uuid4()),
                    share_id=uuid.UUID(fixture["share"]),
                    path="Second.md",
                    record={"type": "markdown"},
                    deleted=False,
                )
                db.add(second)
                db.commit()
                fixture["secondEntry"] = second.id
        if args.pictures_endpoint:
            from PIL import Image, PngImagePlugin

            picture_path = temp / "picture.png"
            info = PngImagePlugin.PngInfo()
            info.add_text("private", "Remove fixture metadata")
            with Image.new("RGBA", (800, 600), (40, 80, 120, 160)) as image:
                image.save(picture_path, format="PNG", pnginfo=info)
            fixture["picturePath"] = str(picture_path)
            with session.get_sessionmaker()() as db:
                scope = m.DiscussionScope(
                    share_id=uuid.UUID(fixture["share"]), entry_id=fixture["entry"], kind="note"
                )
                db.add(scope)
                db.flush()
                expired_id = uuid.uuid4()
                raw = picture_path.read_bytes()
                db.add(
                    m.DiscussionPicture(
                        id=expired_id,
                        scope_id=scope.id,
                        owner_id=uuid.UUID(fixture["reviewer"]),
                        state="expired",
                        source_sha256=hashlib.sha256(raw).hexdigest(),
                        source_size=len(raw),
                        created_at=security.utcnow() - timedelta(days=2),
                        expires_at=security.utcnow() - timedelta(days=1),
                    )
                )
                db.commit()
                fixture["expiredPictureId"] = str(expired_id)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fixture_path.chmod(0o600)
        process = None
        picture_store = None
        bucket_created = False
        try:
            if args.pictures_endpoint:
                from app.clients.storage_client import get_minio_client

                picture_store = get_minio_client()
                bucket = get_settings().collaboration_pictures_bucket
                if picture_store.bucket_exists(bucket):
                    raise RuntimeError("Expected a new disposable picture bucket")
                picture_store.make_bucket(bucket)
                bucket_created = True
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
                        "on" if args.inbox else "off",
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                ready(number, process)
                result = subprocess.run(
                    [
                        "npm",
                        "test",
                        "--",
                        "--runInBand",
                        "__tests__/collaboration/realDiscussionExportStack.test.ts"
                        if args.export
                        else "__tests__/collaboration/realInboxStack.test.ts"
                        if args.inbox
                        else "__tests__/collaboration/realDiscussionSearchStack.test.ts"
                        if args.search
                        else "__tests__/collaboration/realPictureStack.test.ts"
                        if args.pictures_endpoint
                        else "__tests__/collaboration/realDiscussionStack.test.ts",
                    ],
                    cwd=args.plugin.resolve(),
                    env={
                        **os.environ,
                        "EVC_EXPORT_FIXTURE"
                        if args.export
                        else "EVC_INBOX_FIXTURE"
                        if args.inbox
                        else "EVC_SEARCH_FIXTURE"
                        if args.search
                        else "EVC_PICTURE_FIXTURE"
                        if args.pictures_endpoint
                        else "EVC_DISCUSSION_FIXTURE": str(fixture_path),
                    },
                    timeout=90,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError("Discussion client integration failed")
        finally:
            stop(process)
            session.get_engine().dispose()
            if picture_store and bucket_created:
                for obj in picture_store.list_objects(bucket, recursive=True):
                    picture_store.remove_object(bucket, obj.object_name)
                picture_store.remove_bucket(bucket)
    print(
        "PASS: actual discussion archive snapshot, picture files, hashes and permission rechecks"
        if args.export
        else "PASS: actual application inbox worker, HTTP client and offline recovery"
        if args.inbox
        else "PASS: actual authorized search, edits, filters, navigation and revocation"
        if args.search
        else "PASS: real picture HTTP/outbox and MinIO proof"
        if args.pictures_endpoint
        else "PASS: real HTTP outbox retry, replies/history/reactions, revocation and recovery"
    )


if __name__ == "__main__":
    main()

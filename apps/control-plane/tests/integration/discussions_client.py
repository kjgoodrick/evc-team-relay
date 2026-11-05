"""Run the real plugin outbox against a disposable loopback control plane.

SQLite validates the HTTP/client contract here; discussions_postgres.py covers
database locking/migrations. No real vault, relay, credentials or service is used.
"""

import argparse
import json
import os
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
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="evc-discussion-client-") as temporary:
        temp, number = Path(temporary), port()
        os.environ.update(
            DATABASE_URL="sqlite+pysqlite:///" + str(temp / "test.db"),
            JWT_SECRET=secrets.token_hex(32),
            COLLABORATION_POLICY_ENABLED="true",
            COLLABORATION_DISCUSSIONS_ENABLED="true",
            LOG_LEVEL="WARNING",
            CONTROL_PLANE_PUBLIC_URL=f"http://127.0.0.1:{number}",
        )
        from app.core import security
        from app.core.config import get_settings

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
                "token": security.create_access_token(
                    str(reviewer.id), session_id=str(reviewer_session.id)
                ),
                "ownerToken": security.create_access_token(
                    str(owner.id), session_id=str(owner_session.id)
                ),
            }
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fixture_path.chmod(0o600)
        process = None
        try:
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
                result = subprocess.run(
                    [
                        "npm",
                        "test",
                        "--",
                        "--runInBand",
                        "__tests__/collaboration/realDiscussionStack.test.ts",
                    ],
                    cwd=args.plugin.resolve(),
                    env={**os.environ, "EVC_DISCUSSION_FIXTURE": str(fixture_path)},
                    timeout=90,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError("Discussion client integration failed")
        finally:
            stop(process)
            session.get_engine().dispose()
    print("PASS: real HTTP outbox retry, replies/history/reactions, revocation and local recovery")


if __name__ == "__main__":
    main()

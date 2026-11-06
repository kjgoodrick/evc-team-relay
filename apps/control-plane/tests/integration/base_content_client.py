"""Exercise the actual plugin Bases transport/store over disposable loopback HTTP.

SQLite proves the client wire/persistence contract; bases_registry_stack.py
separately proves PostgreSQL serialization. No vault or existing service is used.
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
    with tempfile.TemporaryDirectory(prefix="evc-base-content-client-") as temporary:
        temp, number = Path(temporary), port()
        os.environ.update(
            DATABASE_URL="sqlite+pysqlite:///" + str(temp / "test.db"),
            JWT_SECRET=secrets.token_hex(32),
            MANAGED_BASES_SYNC_ENABLED="true",
            COLLABORATION_POLICY_ENABLED="true",
            COLLABORATION_BASES_REGISTRY_ENABLED="true",
            COLLABORATION_DISCUSSIONS_ENABLED="true",
            COLLABORATION_BASES_DISCUSSIONS_ENABLED="true",
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
            owner = m.User(email="owner@bases.fixture", password_hash="", is_active=True)
            reviewer = m.User(email="reviewer@bases.fixture", password_hash="", is_active=True)
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
            entry_id = str(uuid.uuid4())
            registry_entry_id = str(uuid.uuid4())
            discussion_entry_id, record_id = str(uuid.uuid4()), str(uuid.uuid4())
            session_entry_id = str(uuid.uuid4())
            db.add_all(
                [
                    m.ManagedEntry(
                        id=entry_id,
                        share_id=share.id,
                        path="Planning.base",
                        deleted=False,
                        record={"id": entry_id, "version": 0, "type": "file"},
                    ),
                    m.ManagedEntry(
                        id=registry_entry_id,
                        share_id=share.id,
                        path="Registry.base",
                        deleted=False,
                        record={"id": registry_entry_id, "version": 0, "type": "file"},
                    ),
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
            for identity, path, kind in [
                (discussion_entry_id, "Discussions.base", "file"),
                (session_entry_id, "Sessions.base", "file"),
                (record_id, "Record.md", "markdown"),
            ]:
                db.add(
                    m.ManagedEntry(
                        id=identity,
                        share_id=share.id,
                        path=path,
                        deleted=False,
                        record={"id": identity, "version": 0, "type": kind},
                    )
                )
            owner_session, _ = session_service.create_session(db, owner.id)
            reviewer_session, _ = session_service.create_session(db, reviewer.id)
            db.commit()
            fixture = dict(
                base=f"http://127.0.0.1:{number}",
                share=str(share.id),
                entry=entry_id,
                registryEntry=registry_entry_id,
                discussionEntry=discussion_entry_id,
                sessionEntry=session_entry_id,
                record=record_id,
                owner=str(owner.id),
                reviewer=str(reviewer.id),
                ownerToken=security.create_access_token(
                    str(owner.id), session_id=str(owner_session.id)
                ),
                reviewerToken=security.create_access_token(
                    str(reviewer.id), session_id=str(reviewer_session.id)
                ),
            )
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
                        "__tests__/bases/realBaseContent.test.ts",
                        "__tests__/bases/realBaseRegistry.test.ts",
                        "__tests__/collaboration/realBaseDiscussionStack.test.ts",
                    ],
                    cwd=args.plugin.resolve(),
                    env={**os.environ, "EVC_BASE_CONTENT_FIXTURE": str(fixture_path)},
                    timeout=90,
                )
                if result.returncode:
                    raise RuntimeError("Bases client HTTP proof failed")
                print(
                    "PASS: actual Bases client HTTP, offline persistence, exact retry, "
                    "conflicts, withdrawal, saved-view identity, repair recovery and discussions"
                )
        finally:
            if process:
                stop(process)
            session.get_engine().dispose()
    print("Disposable HTTP process and SQLite fixture removed; no native vault was used")


if __name__ == "__main__":
    main()

"""Recovery migration, audit commit and revocation locks on disposable PostgreSQL."""

import importlib
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from base_discussions_postgres import wait_for_lock
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussion_moderation import ModerationRead
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_moderation as moderation
from app.services import discussion_service as discussions
from app.services import session_service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable loopback collaboration_test database is allowed")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    with engine.connect() as db:
        assert not db.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ), "Expected an empty database"
    with engine.begin() as db:
        labels = ",".join(
            "'" + item.value + "'"
            for item in m.AuditAction
            if item != m.AuditAction.DISCUSSION_RECOVERY_READ
        )
        db.execute(text("CREATE TYPE auditaction AS ENUM (" + labels + ")"))
    m.Base.metadata.create_all(engine)
    settings = get_settings()
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_moderation_recovery_enabled = True
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@export.test", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@export.test", password_hash="", is_active=True)
        db.add_all([owner, reviewer])
        db.flush()
        share = m.Share(
            kind=m.ShareKind.FOLDER,
            path="Fixture",
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
            collaboration_generation=uuid.uuid4(),
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
            ]
        )
        tokens = {}
        for user in [owner, reviewer]:
            login, _ = session_service.create_session(db, user.id)
            tokens[user.id] = security.create_access_token(str(user.id), session_id=str(login.id))
        db.commit()
        share_id, entry_id, owner_id, reviewer_id = share.id, entry.id, owner.id, reviewer.id

    def envelope(command):
        return CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=command,
            )
        )

    def body(value):
        return {
            "version": 1,
            "blocks": [
                {"type": "paragraph", "children": [{"type": "text", "text": value, "marks": []}]}
            ],
        }

    tid, mid = uuid.uuid4(), uuid.uuid4()
    with Session(engine, autoflush=False) as db:
        discussions.apply_command(
            db,
            tokens[owner_id],
            envelope(
                dict(
                    type="thread.create",
                    thread_id=tid,
                    message_id=mid,
                    anchor=dict(version=1, type="note"),
                    body=body("before"),
                )
            ),
        )

    with Session(engine, autoflush=False) as db:
        discussions.apply_command(
            db,
            tokens[owner_id],
            envelope(
                dict(
                    type="message.delete",
                    thread_id=tid,
                    message_id=mid,
                    expected_revision=1,
                )
            ),
        )
        removed_at = db.get(m.DiscussionMessage, mid).deleted_at
    migration = importlib.import_module(
        "app.db.migrations.versions.202611070003_discussion_moderation_recovery"
    )
    with engine.begin() as db:
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
            migration.upgrade()
    with Session(engine) as db:
        assert db.get(m.DiscussionMessage, mid).deleted_at == removed_at
        assert db.get(m.DiscussionMessageRevision, (mid, 1)).body == body("before")
    request = ModerationRead(reason="Synthetic PostgreSQL recovery proof")

    def count_audits(db):
        return db.scalar(
            select(func.count())
            .select_from(m.AuditLog)
            .where(m.AuditLog.action == m.AuditAction.DISCUSSION_RECOVERY_READ)
        )

    with Session(engine, autoflush=False) as db:

        def fail_commit():
            db.flush()  # Prove an inserted audit is rolled back before disclosure.
            raise OperationalError("synthetic commit failure", {}, None)

        with patch.object(db, "commit", fail_commit):
            try:
                moderation.history(db, tokens[owner_id], share_id, entry_id, tid, mid, request)
                raise AssertionError("Audit commit failure returned content")
            except HTTPException as exc:
                assert exc.status_code == 503
    with Session(engine) as db:
        assert count_audits(db) == 0
    entered, release, pid = threading.Event(), threading.Event(), Queue()
    original = moderation.JSONResponse

    def pause(*args, **kwargs):
        response = original(*args, **kwargs)
        assert b"before" in response.body
        entered.set()
        assert release.wait(8)
        return response

    def capture():
        with Session(engine, autoflush=False) as db:
            response = moderation.history(
                db, tokens[owner_id], share_id, entry_id, tid, mid, request
            )
            page = json.loads(response.body)
        with Session(engine) as verify:
            assert verify.get(m.AuditLog, uuid.UUID(page["audit_id"])) is not None
        return page

    def revoke():
        with Session(engine) as db:
            pid.put(db.scalar(text("SELECT pg_backend_pid()")))
            db.execute(delete(m.UserSession).where(m.UserSession.user_id == owner_id))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool, patch.object(moderation, "JSONResponse", pause):
        reading = pool.submit(capture)
        assert entered.wait(5)
        writer = pool.submit(revoke)
        try:
            wait_for_lock(engine, pid.get(timeout=5))
            with Session(engine) as db:
                assert count_audits(db) == 0
            assert not writer.done()
        finally:
            release.set()
        assert reading.result(timeout=8)["revisions"][0]["body"] == body("before")
        writer.result(timeout=8)
    with Session(engine, autoflush=False) as db:
        try:
            moderation.history(db, tokens[owner_id], share_id, entry_id, tid, mid, request)
            raise AssertionError("Revoked session recovered content")
        except HTTPException as exc:
            assert exc.status_code == 401
    with Session(engine, autoflush=False) as db:
        login, _ = session_service.create_session(db, owner_id)
        catalog_token = security.create_access_token(str(owner_id), session_id=str(login.id))
        db.commit()
        response = moderation.catalog(db, catalog_token, share_id, entry_id, request)
        catalog = json.loads(response.body)
        assert [item["message_id"] for item in catalog["items"]] == [str(mid)]
        assert b"before" not in response.body and catalog["view_id"] is None
    with Session(engine) as db:
        audit = db.get(m.AuditLog, uuid.UUID(catalog["audit_id"]))
        assert audit.details["resource"] == "removed_catalog"
        assert audit.details["messages"] == [{"thread_id": str(tid), "message_id": str(mid)}]
    engine.dispose()
    print(
        "PASS: PostgreSQL old audit enum upgrade, removal-clock backfill, audit-before-disclosure, "
        "commit failure rollback, session revocation lock and subsequent refusal, "
        "bounded removed-content catalog and committed metadata audit"
    )


if __name__ == "__main__":
    main()

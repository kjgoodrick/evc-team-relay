"""Retention purge, picture references and concurrent recovery on disposable PostgreSQL."""

import importlib
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussion_moderation import ModerationRead
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_moderation as moderation
from app.services import discussion_retention as retention
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
    index = importlib.import_module(
        "app.db.migrations.versions.202611070004_discussion_retention_index"
    )
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        index.downgrade()
        index.upgrade()
    settings.collaboration_removed_content_cleanup_enabled = True
    factory = sessionmaker(engine, autoflush=False)
    future = security.utcnow() + timedelta(days=31)
    request = ModerationRead(reason="Synthetic concurrent recovery before retention")
    entered, release = threading.Event(), threading.Event()
    original_response = moderation.JSONResponse

    def pause(*args, **kwargs):
        response = original_response(*args, **kwargs)
        entered.set()
        assert release.wait(8)
        return response

    def read():
        with Session(engine, autoflush=False) as db:
            return moderation.history(db, tokens[owner_id], share_id, entry_id, tid, mid, request)

    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(moderation, "JSONResponse", pause):
        reading = pool.submit(read)
        assert entered.wait(5)
        try:
            counts = retention.run_batch(factory=factory, now=future)
            assert counts.busy == 1 and counts.messages == 0
            with Session(engine) as db:
                assert db.get(m.DiscussionMessageRevision, (mid, 1)).body == body("before")
        finally:
            release.set()
        assert json.loads(reading.result(timeout=8).body)["revisions"][0]["body"] == body("before")

    # Exercise the real SQL NOT EXISTS/update/delete path for shared picture IDs.
    pid, second_id = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as db:
        scope = db.scalar(select(m.DiscussionScope).where(m.DiscussionScope.entry_id == entry_id))
        picture = m.DiscussionPicture(
            id=pid,
            scope_id=scope.id,
            owner_id=owner_id,
            state="attached",
            source_sha256="a" * 64,
            source_size=1,
            source_mime="image/png",
            renditions={},
            created_at=security.utcnow(),
            expires_at=security.utcnow(),
        )
        second = m.DiscussionMessage(
            id=second_id,
            thread_id=tid,
            author_id=owner_id,
            revision=1,
            deleted=False,
            created_sequence=3,
        )
        db.add_all([picture, second])
        db.flush()
        with_picture = body("before")
        with_picture["blocks"].append(dict(type="picture", picture_id=str(pid)))
        db.get(m.DiscussionMessageRevision, (mid, 1)).body = with_picture
        db.add(m.DiscussionMessageRevision(message_id=second_id, revision=1, body=with_picture))
        db.flush()
        db.add_all(
            [
                m.DiscussionPictureReference(picture_id=pid, message_id=message_id, revision=1)
                for message_id in [mid, second_id]
            ]
        )
        db.commit()
    first = retention.run_batch(factory=factory, now=future)
    assert first.messages == 1 and first.picture_identities_expired == 0
    with Session(engine) as db:
        assert db.get(m.DiscussionPicture, pid).state == "attached"
        assert db.get(m.DiscussionMessageRevision, (mid, 1)).body is None
        assert db.get(m.DiscussionMessageRevision, (second_id, 1)).body == with_picture
        second = db.get(m.DiscussionMessage, second_id)
        second.deleted = True
        second.deleted_at = removed_at
        db.commit()
    with patch.object(
        retention, "finish_thread", side_effect=RuntimeError("synthetic purge failure")
    ):
        try:
            retention.run_batch(factory=factory, now=future)
            raise AssertionError("Expected a purge rollback")
        except RuntimeError as exc:
            assert "synthetic purge failure" in str(exc)
    with Session(engine) as db:
        assert db.get(m.DiscussionPicture, pid).state == "attached"
        assert db.get(m.DiscussionMessageRevision, (second_id, 1)).body == with_picture
        assert db.get(m.DiscussionPictureReference, (pid, second_id, 1)) is not None
        assert db.get(m.DiscussionMessage, second_id).content_purged_at is None
    second = retention.run_batch(factory=factory, now=future)
    assert second.messages == 1 and second.picture_identities_expired == 1
    with Session(engine) as db:
        assert db.get(m.DiscussionPicture, pid).state == "expired"
        assert db.get(m.DiscussionPictureReference, (pid, second_id, 1)) is None
        assert db.get(m.DiscussionMessageRevision, (second_id, 1)).body is None
    try:
        read()
        raise AssertionError("Purged content was recovered")
    except HTTPException as exc:
        assert exc.status_code == 404
    assert retention.run_batch(factory=factory, now=future).messages == 0
    engine.dispose()
    print(
        "PASS: PostgreSQL retention index, skip locked active recovery, body/reference rollback, "
        "live shared-picture protection, last-reference expiry and purged recovery refusal"
    )


if __name__ == "__main__":
    main()

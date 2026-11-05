"""Disposable PostgreSQL migration + optimistic-concurrency check (no production DSN).

COLLABORATION_TEST_DATABASE_URL=postgresql+psycopg://.../collaboration_test \
    python tests/integration/comment_policy_postgres.py
"""

import importlib.util
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.db import models
from app.schemas.collaboration import CommentRuleUpdate
from app.services import collaboration_policy


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable localhost collaboration_test database is allowed")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    # Require an empty dedicated database; never erase an existing test/service database.
    with engine.connect() as conn:
        if conn.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable test database")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = models.User(email="owner@fixture.test", password_hash="", is_active=True)
        reviewer = models.User(email="reviewer@fixture.test", password_hash="", is_active=True)
        db.add_all([owner, reviewer])
        db.flush()
        share = models.Share(
            kind=models.ShareKind.FOLDER,
            path="Fixture",
            managed=True,
            visibility=models.ShareVisibility.PRIVATE,
            owner_user_id=owner.id,
        )
        db.add(share)
        db.flush()
        db.add(
            models.ShareMember(
                share_id=share.id, user_id=reviewer.id, role=models.ShareMemberRole.VIEWER
            )
        )
        db.add(
            models.ManagedRule(
                share_id=share.id, user_id=reviewer.id, path="", recursive=True, permission="read"
            )
        )
        note_id = str(uuid.uuid4())
        db.add(
            models.ManagedEntry(
                id=note_id,
                share_id=share.id,
                path="Note.md",
                record={"type": "markdown"},
                deleted=False,
            )
        )
        db.commit()
        owner_id, reviewer_id, share_id = owner.id, reviewer.id, share.id

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "app/db/migrations/versions/202611050001_comment_permissions.py"
    )
    spec = importlib.util.spec_from_file_location("comment_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.downgrade()  # Establish the immediately preceding schema.
        assert conn.scalar(text("SELECT count(*) FROM managed_rules")) == 1
        migration.upgrade()
        assert conn.scalar(text("SELECT comment_rules_revision FROM shares")) == 0
        assert conn.scalar(text("SELECT count(*) FROM managed_rules")) == 1

    barrier = threading.Barrier(2)

    def writer(allowed):
        with Session(engine) as db:
            # Both callers have observed the same revision before either gets the lock.
            share = db.get(models.Share, share_id)
            owner = db.get(models.User, owner_id)
            barrier.wait(timeout=10)
            try:
                result = collaboration_policy.replace_rules(
                    db,
                    share,
                    owner,
                    CommentRuleUpdate(
                        expected_revision=0,
                        rules=[dict(user_id=reviewer_id, path="", allowed=allowed)],
                    ),
                )
                return (200, result.revision)
            except HTTPException as error:
                db.rollback()
                return (error.status_code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, [True, False]))
    assert sorted(status for status, _ in results) == [200, 409], results
    with Session(engine) as db:
        share = db.get(models.Share, share_id)
        reviewer = db.get(models.User, reviewer_id)
        assert share.comment_rules_revision == 1
        rule = db.scalar(select(models.ManagedCommentRule))
        capabilities = collaboration_policy.entry_capabilities(db, share, reviewer, note_id)
        assert capabilities.read and not capabilities.edit
        assert capabilities.comment == rule.allowed
        assert len(list(db.scalars(select(models.AuditLog)))) == 1
    # GET must refresh a stale ORM object and hold its revision/rule snapshot
    # against writers until its transaction ends.
    with Session(engine) as reader, Session(engine) as writer_db:
        stale_share = reader.get(models.Share, share_id)
        set_committed_value(stale_share, "comment_rules_revision", 0)
        result = collaboration_policy.get_rules(
            reader, stale_share, reader.get(models.User, owner_id)
        )
        assert result.revision == 1 and len(result.rules) == 1
        try:
            writer_db.scalar(
                select(models.Share).where(models.Share.id == share_id).with_for_update(nowait=True)
            )
        except OperationalError as error:
            assert error.orig.sqlstate == "55P03", error
            writer_db.rollback()
        else:
            raise AssertionError("Policy reader did not protect its revision/rule snapshot")
        reader.rollback()
        assert (
            writer_db.scalar(
                select(models.Share).where(models.Share.id == share_id).with_for_update(nowait=True)
            )
            is not None
        )
    engine.dispose()
    print(
        "PASS: PostgreSQL migration preserves ACLs; competing saves serialize as 200/409; "
        "reviewer remains read-only; policy reads hold a coherent revision/rule snapshot"
    )


if __name__ == "__main__":
    main()

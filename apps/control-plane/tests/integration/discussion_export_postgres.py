"""Read-lock/permission evidence on an empty disposable loopback PostgreSQL DB."""

import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from base_discussions_postgres import wait_for_lock
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_export as export
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
    m.Base.metadata.create_all(engine)
    settings = get_settings()
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_export_enabled = True
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
            "blocks": [{"type": "paragraph", "children": [{"type": "text", "text": value}]}],
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

    def capture():
        with Session(engine, autoflush=False) as db:
            return json.loads(export.snapshot(db, tokens[reviewer_id], share_id, entry_id))

    original = export._thread
    for change in ["edit", "permission"]:
        entered, release = threading.Event(), threading.Event()
        pid = Queue()

        def pause(*args):
            entered.set()
            assert release.wait(8), "export release timed out"
            return original(*args)

        def writer():
            with Session(engine, autoflush=False) as db:
                pid.put(db.scalar(text("SELECT pg_backend_pid()")))
                if change == "edit":
                    discussions.apply_command(
                        db,
                        tokens[owner_id],
                        envelope(
                            dict(
                                type="message.edit",
                                thread_id=tid,
                                message_id=mid,
                                expected_revision=1,
                                body=body("after"),
                            )
                        ),
                    )
                else:
                    db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
                    rule = db.scalar(
                        select(m.ManagedRule).where(
                            m.ManagedRule.share_id == share_id, m.ManagedRule.user_id == reviewer_id
                        )
                    )
                    rule.permission = "none"
                    db.commit()

        with (
            patch.object(export, "_thread", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            reading = pool.submit(capture)
            assert entered.wait(5)
            writing = pool.submit(writer)
            try:
                wait_for_lock(engine, pid.get(timeout=5))
                assert not writing.done()
            finally:
                release.set()
            archive = reading.result(timeout=10)
            writing.result(timeout=10)
        revisions = [row for row in archive["records"] if row["type"] == "message_revision"]
        assert len(revisions) == (1 if change == "edit" else 2)
        assert archive["sequence"] == (1 if change == "edit" else 2)
        print("PASS: export holds a consistent read boundary while", change, "waits")
    try:
        capture()
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Revoked reader exported a scope")
    print("PASS: subsequent export denies the revoked reader")

    def measured_capture():
        queries = []

        def count(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        started = time.monotonic()
        try:
            with Session(engine, autoflush=False) as db:
                encoded = export.snapshot(db, tokens[owner_id], share_id, entry_id)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return encoded, queries, round((time.monotonic() - started) * 1000, 1)

    _, baseline_queries, _ = measured_capture()
    # Exercise server-side streaming across multiple fetch batches. Compare
    # against the same authenticated scope before replies, so this proves that
    # query count is independent of reply count rather than guessing a ceiling.
    for number in range(120):
        with Session(engine, autoflush=False) as db:
            discussions.apply_command(
                db,
                tokens[owner_id],
                envelope(
                    dict(
                        type="reply.create",
                        thread_id=tid,
                        message_id=uuid.uuid4(),
                        body=body("Synthetic archived reply " + str(number)),
                    )
                ),
            )
    encoded, queries, elapsed_ms = measured_capture()
    value = json.loads(encoded)
    assert len([row for row in value["records"] if row["type"] == "message"]) == 121
    assert len(queries) == len(baseline_queries), (len(queries), len(baseline_queries))
    print(
        "PASS: 121-message archive streams in",
        len(queries),
        "SELECTs (same as one message);",
        len(encoded),
        "bytes;",
        elapsed_ms,
        "ms on disposable local PostgreSQL",
    )
    entered, release = threading.Event(), threading.Event()
    catalog_pid = Queue()
    original_authorize = export.base_discussions.can_read_thread

    def pause_catalog(*args, **kwargs):
        entered.set()
        assert release.wait(8), "catalog release timed out"
        return original_authorize(*args, **kwargs)

    def catalog_capture():
        with Session(engine, autoflush=False) as db:
            return json.loads(export.catalog(db, tokens[owner_id], share_id))

    def catalog_writer():
        with Session(engine, autoflush=False) as db:
            catalog_pid.put(db.scalar(text("SELECT pg_backend_pid()")))
            discussions.apply_command(
                db,
                tokens[owner_id],
                envelope(
                    dict(
                        type="reply.create",
                        thread_id=tid,
                        message_id=uuid.uuid4(),
                        body=body("After catalog"),
                    )
                ),
            )

    with (
        patch.object(export.base_discussions, "can_read_thread", side_effect=pause_catalog),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        reading = pool.submit(catalog_capture)
        assert entered.wait(5)
        writing = pool.submit(catalog_writer)
        try:
            wait_for_lock(engine, catalog_pid.get(timeout=5))
            assert not writing.done()
        finally:
            release.set()
        inventory = reading.result(timeout=10)
        writing.result(timeout=10)
    assert inventory["sequence"] == value["sequence"]
    assert len(inventory["scopes"]) == 1
    assert inventory["scopes"][0]["entry_id"] == entry_id
    print("PASS: scope catalog is encoded before a waiting discussion writer commits")
    engine.dispose()


if __name__ == "__main__":
    main()

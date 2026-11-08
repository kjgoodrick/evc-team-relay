"""Snapshot consistency and revocation on an empty disposable loopback PostgreSQL DB."""

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
from app.schemas.discussions import CommandEnvelope, MessageBody
from app.services import discussion_service as discussions
from app.services import discussion_snapshot as snapshot
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
    settings.collaboration_snapshot_enabled = True
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@snapshot.test", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@snapshot.test", password_hash="", is_active=True)
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

    def capture(actor=reviewer_id):
        with Session(engine, autoflush=False) as db:
            return json.loads(snapshot.snapshot(db, tokens[actor], share_id, entry_id))

    original = snapshot.json.dumps
    for change in ["edit", "permission"]:
        entered, release = threading.Event(), threading.Event()
        pid = Queue()

        def pause(value, *args, **kwargs):
            # Hold at final serialization: rows and watermark are already read,
            # but the snapshot transaction must still exclude writers.
            if isinstance(value, dict) and "through_sequence" in value:
                entered.set()
                assert release.wait(8), "snapshot release timed out"
            return original(value, *args, **kwargs)

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
                            m.ManagedRule.share_id == share_id,
                            m.ManagedRule.user_id == reviewer_id,
                        )
                    )
                    rule.permission = "none"
                    db.commit()

        with (
            patch.object(snapshot.json, "dumps", side_effect=pause),
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
            baseline = reading.result(timeout=10)
            writing.result(timeout=10)
        expected_sequence = 1 if change == "edit" else 2
        assert baseline["through_sequence"] == expected_sequence
        assert baseline["threads"][0]["messages"][0]["body"] == MessageBody.model_validate(
            body("before" if change == "edit" else "after")
        ).model_dump(mode="json")
        if change == "edit":
            with Session(engine, autoflush=False) as db:
                tail = discussions.changes(
                    db, tokens[reviewer_id], share_id, entry_id, cursor=baseline["cursor"]
                )
            assert [row["sequence"] for row in tail["events"]] == [2]
        print("PASS: snapshot locks survive final serialization while", change, "waits", flush=True)
    try:
        capture()
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Revoked reader received a snapshot")
    print(
        "PASS: exact current-state/catch-up boundary; subsequent read denies revoked access",
        flush=True,
    )

    def measured_capture():
        queries = []

        def count(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        started = time.monotonic()
        try:
            value = capture(owner_id)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return value, len(queries), round((time.monotonic() - started) * 1000, 1)

    _, initial, _ = measured_capture()
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
                        body=body("Snapshot reply " + str(number)),
                    )
                ),
            )
    value, queries, elapsed_ms = measured_capture()
    assert len(value["threads"][0]["messages"]) == 121
    # Reactions use one query per batch of 50 messages, not one per message.
    assert queries == initial + 2, (queries, initial)
    assert value["through_sequence"] == 122
    print(
        "PASS: 121 current messages in",
        queries,
        "SELECTs;",
        elapsed_ms,
        "ms on disposable local PostgreSQL",
        flush=True,
    )
    engine.dispose()


if __name__ == "__main__":
    main()

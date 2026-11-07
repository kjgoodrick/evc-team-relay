"""Inbox migration, lease, transaction and read races on disposable PostgreSQL."""

import os
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from base_discussions_postgres import wait_for_lock
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussion_inbox import InboxUpdate, SubscriptionUpdate
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_inbox as inbox
from app.services import discussion_service as discussions
from app.services import session_service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable localhost collaboration_test database is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as connection:
        if connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
    settings = get_settings()
    settings.database_url = url.render_as_string(hide_password=False)
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_inbox_enabled = True
    factory = sessionmaker(engine, autoflush=False)
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")
        with engine.connect() as connection:
            drift = compare_metadata(MigrationContext.configure(connection), m.Base.metadata)
            assert not [
                item
                for item in drift
                if any(
                    name in str(item)
                    for name in (
                        "discussion_inbox",
                        "discussion_notifications",
                        "discussion_subscriptions",
                    )
                )
            ], drift
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070002"
            )
        command.downgrade(config, "202611060006")
        command.upgrade(config, "head")
        print("PASS: inbox migration matches ORM and empty downgrade/upgrade succeeds")

        with factory.begin() as db:
            author = m.User(email="author@inbox.test", password_hash="", is_active=True)
            recipient = m.User(email="recipient@inbox.test", password_hash="", is_active=True)
            db.add_all([author, recipient])
            db.flush()
            author_id, recipient_id = author.id, recipient.id
            share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Fixture",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=author_id,
            )
            db.add(share)
            db.flush()
            share_id, entry_id = share.id, uuid.uuid4()
            membership = m.ShareMember(
                share_id=share_id, user_id=recipient_id, role=m.ShareMemberRole.VIEWER
            )
            db.add_all(
                [
                    membership,
                    m.ManagedEntry(
                        id=str(entry_id),
                        share_id=share_id,
                        path="Notes/One.md",
                        record={"type": "markdown"},
                        deleted=False,
                    ),
                    m.ManagedRule(
                        share_id=share_id,
                        user_id=recipient_id,
                        path="Notes",
                        recursive=True,
                        permission="read",
                    ),
                ]
            )
            tokens = {}
            for user in (author, recipient):
                session, _ = session_service.create_session(db, user.id)
                tokens[user.id] = security.create_access_token(
                    str(user.id), session_id=str(session.id)
                )
            db.flush()
            membership_id = membership.id

        def message(tid=None):
            return CommandEnvelope.model_validate(
                {
                    "protocol_version": 1,
                    "operation_id": str(uuid.uuid4()),
                    "share_id": str(share_id),
                    "entry_id": str(entry_id),
                    "command": {
                        "type": "reply.create" if tid else "thread.create",
                        "thread_id": str(tid or uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        **({} if tid else {"anchor": {"type": "note", "version": 1}}),
                        "body": {
                            "version": 1,
                            "blocks": [
                                {
                                    "type": "paragraph",
                                    "children": [
                                        {
                                            "type": "text",
                                            "text": "Synthetic notification",
                                            "marks": [],
                                        },
                                        {"type": "mention", "user_id": str(recipient_id)},
                                    ],
                                }
                            ],
                        },
                    },
                }
            )

        def send(payload):
            with factory() as db:
                return discussions.apply_command(db, tokens[author_id], payload)

        def page():
            with factory() as db:
                return inbox.list_items(db, tokens[recipient_id], share_id)

        def total(model):
            with factory() as db:
                return db.scalar(select(func.count()).select_from(model))

        payload = message()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(send, [payload, payload]))
        assert results[0] == results[1]
        tid = uuid.UUID(results[0]["thread_id"])
        assert total(m.DiscussionNotification) == 1
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: inbox.claim(factory), range(2)))
        assert sum(item is not None for item in claims) == 1
        claim = next(item for item in claims if item is not None)

        def fail_commit(db):
            raise RuntimeError("injected projector crash before commit")

        event.listen(factory, "before_commit", fail_commit)
        try:
            try:
                inbox.project(factory, claim)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Commit fault was not exercised")
        finally:
            event.remove(factory, "before_commit", fail_commit)
        assert total(m.DiscussionInboxItem) == 0
        with factory() as db:
            assert db.get(m.DiscussionNotification, claim[:3]).completed_at is None
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(lambda _: inbox.project(factory, claim), range(2))) == [
                False,
                True,
            ]
        assert page()["items"][0]["version"] == 1
        print("PASS: concurrent retries/claims and crash recovery project exactly once")

        for _ in range(2):
            send(message(tid))
        claims = [inbox.claim(factory), inbox.claim(factory)]
        with ThreadPoolExecutor(2) as pool:
            assert all(pool.map(lambda value: inbox.project(factory, value), claims))
        assert page()["items"][0]["version"] == 3
        send(message(tid))
        claim = inbox.claim(factory)
        ready = threading.Barrier(2)

        def mark_seen():
            ready.wait(timeout=5)
            with factory() as db:
                return inbox.update_item(
                    db,
                    tokens[recipient_id],
                    share_id,
                    entry_id,
                    tid,
                    InboxUpdate(seen_version=3, action="read"),
                )

        def project_new():
            ready.wait(timeout=5)
            return inbox.project(factory, claim)

        with ThreadPoolExecutor(2) as pool:
            one, two = pool.submit(mark_seen), pool.submit(project_new)
            one.result(timeout=15)
            assert two.result(timeout=15)
        item = page()["items"][0]
        assert item["version"] == 4 and item["read_version"] == 3 and item["unread"]
        print(
            "PASS: simultaneous projection preserves versions and a concurrent reply stays unread"
        )

        send(message(tid))
        claim = inbox.claim(factory)
        with factory.begin() as guard:
            guard.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(inbox.project, factory, claim)
                try:
                    future.result(timeout=9)
                except OperationalError as exc:
                    assert exc.orig.sqlstate == "55P03"
                else:
                    raise AssertionError("Projector did not respect its lock timeout")
        with factory.begin() as db:
            job = db.get(m.DiscussionNotification, claim[:3])
            assert job.completed_at is None and not job.delivered
            job.lease_until = security.utcnow() - timedelta(seconds=1)
        assert inbox.project(factory, inbox.claim(factory))
        assert page()["items"][0]["version"] == 5
        print("PASS: bounded lock wait retains the candidate for lease-expiry recovery")

        ready = threading.Barrier(2)

        def choose(following):
            ready.wait(timeout=5)
            with factory() as db:
                try:
                    inbox.subscription(
                        db,
                        tokens[recipient_id],
                        share_id,
                        entry_id,
                        tid,
                        SubscriptionUpdate(expected_version=0, following=following, muted=False),
                    )
                    return 200
                except HTTPException as exc:
                    return exc.status_code

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(choose, [True, False])) == [200, 409]
        print("PASS: conflicting subscription updates do not silently overwrite each other")

        for _ in range(50):
            send(message())
        for _ in range(50):
            assert inbox.project(factory, inbox.claim(factory))
        counts, timings = [], []
        for _ in range(3):
            statements = []

            def count_query(_connection, cursor, sql, _params, _context, _many):
                if sql.lstrip().upper().startswith("SELECT"):
                    statements.append(sql)
                if "JOIN discussion_threads" in sql and "discussion_inbox_items" in sql:
                    assert getattr(cursor, "name", None), "Expected a streamed PostgreSQL cursor"

            event.listen(engine, "before_cursor_execute", count_query)
            start = time.perf_counter()
            try:
                result = page()
            finally:
                timings.append(time.perf_counter() - start)
                event.remove(engine, "before_cursor_execute", count_query)
            assert len(result["items"]) == 50 and result["next_cursor"]
            assert len(statements) <= 20
            assert not any("discussion_message_revisions" in sql for sql in statements)
            counts.append(len(statements))
        with factory() as db:
            older = inbox.list_items(
                db, tokens[recipient_id], share_id, cursor=result["next_cursor"]
            )
        assert len(older["items"]) == 1 and older["next_cursor"] is None
        assert older["items"][0]["thread_id"] == str(tid)
        print(
            f"PASS: streamed 50-row page uses {counts} SELECTs, "
            f"median {statistics.median(timings) * 1000:.2f} ms; older-page boundary is intact"
        )

        # Request-scoped rule reuse must start after the share read lock. A
        # waiting inbox request must see a committed permission denial.
        read_pids = Queue()

        def read_backend_pid(connection, _record, _proxy):
            read_pids.put(connection.info.backend_pid)

        with factory.begin() as guard:
            guard.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
            event.listen(engine, "checkout", read_backend_pid)
            read_pool = ThreadPoolExecutor(1)
            read_future = read_pool.submit(page)
            pid = read_pids.get(timeout=5)
            event.remove(engine, "checkout", read_backend_pid)
            wait_for_lock(engine, pid)
            rule = guard.scalar(select(m.ManagedRule).where(m.ManagedRule.user_id == recipient_id))
            rule.permission = "none"
        try:
            assert read_future.result(timeout=15) == {"items": [], "next_cursor": None}
        finally:
            read_pool.shutdown()
        with factory.begin() as db:
            db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
            db.scalar(
                select(m.ManagedRule).where(m.ManagedRule.user_id == recipient_id)
            ).permission = "read"
        assert len(page()["items"]) == 50
        print(
            "PASS: inbox listing waits for permission edits and refreshes rules on the next request"
        )

        send(message(tid))
        claim = inbox.claim(factory)
        # Hold the same authority lock used by permission writers. Projection
        # must wait and then observe revocation rather than pre-lock snapshots.
        pids = Queue()

        def backend_pid(connection, _record, _proxy):
            pids.put(connection.info.backend_pid)

        with factory.begin() as guard:
            guard.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
            event.listen(engine, "checkout", backend_pid)
            pool = ThreadPoolExecutor(1)
            future = pool.submit(inbox.project, factory, claim)
            pid = pids.get(timeout=5)
            event.remove(engine, "checkout", backend_pid)
            wait_for_lock(engine, pid)
            guard.delete(guard.get(m.ShareMember, membership_id))
        try:
            assert future.result(timeout=15) is False
        finally:
            pool.shutdown()
        with factory() as db:
            assert db.get(m.DiscussionInboxItem, (recipient_id, tid)).version == 5
        print("PASS: recipient revocation while projector waits prevents delivery")

        try:
            command.downgrade(config, "202611060006")
        except RuntimeError as exc:
            assert "Export/migrate inbox" in str(exc)
        else:
            raise AssertionError("Populated inbox was discarded")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070002"
            )
        print("PASS: populated downgrade refuses to lose inbox/read/preferences")
    finally:
        # This runner owns the initially empty synthetic schema only.
        with engine.begin() as connection:
            m.Base.metadata.drop_all(connection)
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


if __name__ == "__main__":
    main()

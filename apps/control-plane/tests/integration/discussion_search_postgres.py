"""Disposable PostgreSQL proof: backfill, index, live authorization and rollback.

Requires an empty localhost database named collaboration_test. No real data.
"""

import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussion_search import DiscussionSearch
from app.schemas.discussions import CommandEnvelope
from app.services import (
    discussion_search,
    discussion_search_index,
    discussion_service,
    session_service,
)


def body(value, user_id=None):
    children = [{"type": "text", "text": value}]
    if user_id:
        children.append({"type": "mention", "user_id": str(user_id)})
    return {"version": 1, "blocks": [{"type": "paragraph", "children": children}]}


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only an empty disposable localhost collaboration_test is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            )
            == 0
        )
    settings = get_settings()
    settings.database_url = url.render_as_string(hide_password=False)
    settings.collaboration_policy_enabled = settings.collaboration_discussions_enabled = (
        settings.collaboration_search_enabled
    ) = True
    config = Config("alembic.ini")
    command.upgrade(config, "202611070001")
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@search.fixture", password_hash="", is_active=True)
        reader = m.User(email="reader@search.fixture", password_hash="", is_active=True)
        db.add_all([owner, reader])
        db.flush()
        share = m.Share(
            kind=m.ShareKind.FOLDER,
            path="Team",
            managed=True,
            visibility=m.ShareVisibility.PRIVATE,
            owner_user_id=owner.id,
            collaboration_sequence=3,
            collaboration_epoch=uuid.uuid4(),
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
        db.add(entry)
        db.flush()
        db.add(m.ShareMember(share_id=share.id, user_id=reader.id, role=m.ShareMemberRole.VIEWER))
        rule = m.ManagedRule(
            share_id=share.id, user_id=reader.id, path="", recursive=True, permission="read"
        )
        db.add(rule)
        scope = m.DiscussionScope(share_id=share.id, entry_id=entry.id, kind="note")
        db.add(scope)
        db.flush()
        thread = m.DiscussionThread(
            id=uuid.uuid4(),
            scope_id=scope.id,
            author_id=owner.id,
            created_sequence=1,
            activity_sequence=3,
        )
        db.add(thread)
        db.flush()
        db.add(
            m.DiscussionAnchorRevision(
                thread_id=thread.id,
                revision=1,
                payload={"version": 1, "type": "note", "entry_id": entry.id},
                actor_id=owner.id,
            )
        )
        message = m.DiscussionMessage(
            id=uuid.uuid4(), thread_id=thread.id, author_id=owner.id, revision=2, created_sequence=1
        )
        removed = m.DiscussionMessage(
            id=uuid.uuid4(),
            thread_id=thread.id,
            author_id=owner.id,
            revision=2,
            deleted=True,
            created_sequence=3,
        )
        db.add_all([message, removed])
        db.flush()
        db.add_all(
            [
                m.DiscussionMessageRevision(
                    message_id=message.id,
                    revision=1,
                    body=body("Obsolete private history"),
                    actor_id=owner.id,
                ),
                m.DiscussionMessageRevision(
                    message_id=message.id,
                    revision=2,
                    body=body("Needleunique Straße Café", reader.id),
                    actor_id=owner.id,
                ),
                m.DiscussionMessageRevision(
                    message_id=removed.id,
                    revision=1,
                    body=body("Removed private text"),
                    actor_id=owner.id,
                ),
                m.DiscussionMessageRevision(
                    message_id=removed.id, revision=2, body=None, actor_id=owner.id
                ),
            ]
        )
        tokens = {}
        for user in (owner, reader):
            session, _ = session_service.create_session(db, user.id)
            tokens[user.id] = security.create_access_token(str(user.id), session_id=str(session.id))
        db.commit()
        share_id, entry_id, thread_id, message_id, reader_id, owner_id, rule_id = (
            share.id,
            entry.id,
            thread.id,
            message.id,
            reader.id,
            owner.id,
            rule.id,
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070002"
        drift = compare_metadata(MigrationContext.configure(connection), m.Base.metadata)
        assert not [item for item in drift if "discussion_search" in str(item)], drift
        assert connection.scalar(text("SELECT count(*) FROM discussion_search_documents")) == 1
        assert connection.scalar(text("SELECT count(*) FROM discussion_search_mentions")) == 1

    def find(query="", actor=reader_id, **kwargs):
        with Session(engine, autoflush=False) as db:
            return discussion_search.search(
                db, tokens[actor], share_id, DiscussionSearch(query=query, **kwargs)
            )

    assert find("obsolete")["items"] == [] and find("removed")["items"] == []
    assert find("STRASSE Ｃａｆé", mentions=True)["items"][0]["message_id"] == str(message_id)
    print("PASS: populated upgrade indexes only current bodies and current mentions", flush=True)

    def send(cmd):
        with Session(engine, autoflush=False) as db:
            return discussion_service.apply_command(
                db,
                tokens[owner_id],
                CommandEnvelope.model_validate(
                    {
                        "protocol_version": 1,
                        "operation_id": str(uuid.uuid4()),
                        "share_id": str(share_id),
                        "entry_id": entry_id,
                        "command": cmd,
                    }
                ),
            )

    original = discussion_search_index.update

    def fail_after_projection(db, message, content):
        original(db, message, content)
        raise RuntimeError("Synthetic failure after search projection")

    with patch.object(discussion_search_index, "update", fail_after_projection):
        try:
            send(
                {
                    "type": "message.edit",
                    "thread_id": str(thread_id),
                    "message_id": str(message_id),
                    "expected_revision": 2,
                    "body": body("Mustrollbacksafely"),
                }
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Synthetic failure was not raised")
    assert find("mustrollbacksafely")["items"] == []
    assert find("needleunique")["items"][0]["message_revision"] == 2
    send(
        {
            "type": "message.edit",
            "thread_id": str(thread_id),
            "message_id": str(message_id),
            "expected_revision": 2,
            "body": body("Needleunique current edit"),
        }
    )
    assert find("strasse")["items"] == [] and find(mentions=True)["items"] == []
    assert find("needleunique")["items"][0]["message_revision"] == 3
    print(
        "PASS: command rollback and edits preserve exact index/message revision agreement",
        flush=True,
    )

    # A real concurrent policy writer owns the share lock. The read must wait
    # and then observe the denial, even with a previously valid account session.
    started = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine, autoflush=False) as writer:
            writer.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())

            def blocked_read():
                started.set()
                return find("needleunique")

            reading = pool.submit(blocked_read)
            assert started.wait(2)
            time.sleep(0.15)
            assert not reading.done()
            writer.get(m.ManagedRule, rule_id).permission = "none"
            writer.commit()
        assert reading.result(timeout=5)["items"] == []
    with Session(engine) as db:
        db.get(m.ManagedRule, rule_id).permission = "read"
        db.commit()
    print("PASS: permission writer/read lock ordering prevents stale search results", flush=True)

    # Synthetic corpus for an actual selective GIN plan, not a release latency
    # claim. It is created only in this disposable database.
    with engine.begin() as connection:
        messages, documents = [], []
        for n in range(3000):
            mid = uuid.uuid4()
            messages.append(
                {
                    "id": mid,
                    "thread_id": thread_id,
                    "author_id": owner_id,
                    "revision": 1,
                    "deleted": False,
                    "created_sequence": 10 + n,
                }
            )
            documents.append(
                {
                    "message_id": mid,
                    "revision": 1,
                    "plain_text": f"Synthetic filler {n}",
                    "terms": f"synthetic filler {n}",
                }
            )
        connection.execute(m.DiscussionMessage.__table__.insert(), messages)
        connection.execute(m.DiscussionSearchDocument.__table__.insert(), documents)
        connection.execute(text("ANALYZE discussion_search_documents"))
        plan = connection.scalar(
            text(
                "EXPLAIN (ANALYZE, FORMAT JSON) SELECT message_id FROM discussion_search_documents "
                "WHERE to_tsvector('simple'::regconfig, terms) "
                "@@ plainto_tsquery('simple'::regconfig, 'needleunique')"
            )
        )
        assert "ix_discussion_search_terms" in str(plan), plan
        assert plan[0]["Plan"]["Actual Rows"] == 1
    print(
        "PASS: selective full-text query uses the PostgreSQL GIN index over 3,001 documents",
        flush=True,
    )

    # Drop/rebuild only the derived search projection; authoritative history is
    # preserved. Synthetic corpus has no revisions and is intentionally absent
    # from the backfill; only the real current message is rebuilt.
    command.downgrade(config, "202611070001")
    command.upgrade(config, "head")
    assert find("needleunique")["items"][0]["message_revision"] == 3
    assert find("obsolete")["items"] == []
    print("PASS: populated index downgrade/rebuild preserves current-message behavior", flush=True)
    engine.dispose()


if __name__ == "__main__":
    main()

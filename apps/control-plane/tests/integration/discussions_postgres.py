"""Disposable PostgreSQL proof for discussion transactions, migration and auth guards.

Requires an empty localhost database named collaboration_test. No production data.
COLLABORATION_TEST_DATABASE_URL=postgresql+psycopg://.../collaboration_test \
    python tests/integration/discussions_postgres.py
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
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service, session_service, share_service, user_service
from app.services import discussion_service as service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable localhost collaboration_test database is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as conn:
        if conn.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
    get_settings().database_url = url.render_as_string(hide_password=False)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "202611070001"
        diffs = compare_metadata(MigrationContext.configure(conn), m.Base.metadata)

        def relevant(diff):
            # Isolate schema parity for this additive migration; unrelated
            # historical enum/default drift is outside this proof.
            return (
                "discussion_" in str(diff)
                or "collaboration_sequence" in str(diff)
                or "collaboration_epoch" in str(diff)
                or "collaboration_generation" in str(diff)
                or "collaboration_base" in str(diff)
                or "managed_base_" in str(diff)
            )

        assert not [d for d in diffs if relevant(d)], diffs
    # Empty-table downgrade/upgrade exercises the inverse migration too.
    command.downgrade(config, "202611050001")
    command.upgrade(config, "head")
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@fixture.test", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@fixture.test", password_hash="", is_active=True)
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
        member = m.ShareMember(
            share_id=share.id, user_id=reviewer.id, role=m.ShareMemberRole.VIEWER
        )
        db.add(member)
        db.add(
            m.ManagedRule(
                share_id=share.id, user_id=reviewer.id, path="", recursive=True, permission="read"
            )
        )
        db.add(
            m.ManagedCommentRule(
                share_id=share.id, user_id=reviewer.id, path="", recursive=True, allowed=True
            )
        )
        entry = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=share.id,
            path="Note.md",
            record={"type": "markdown"},
            deleted=False,
        )
        db.add(entry)
        session, _ = session_service.create_session(db, reviewer.id)
        db.commit()
        share_id, entry_id, reviewer_id, member_id, session_id = (
            share.id,
            uuid.UUID(entry.id),
            reviewer.id,
            member.id,
            session.id,
        )
        token = security.create_access_token(str(reviewer_id), session_id=str(session_id))
    body = {
        "version": 1,
        "blocks": [
            {"type": "paragraph", "children": [{"type": "text", "text": "Synthetic comment"}]}
        ],
    }

    def envelope(cmd):
        return CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=cmd,
            )
        )

    create = envelope(
        dict(
            type="thread.create",
            thread_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            anchor=dict(version=1, type="note"),
            body=body,
        )
    )
    barrier = threading.Barrier(2)

    def concurrent_send(payload):
        with Session(engine, autoflush=False) as db:
            barrier.wait(timeout=10)
            try:
                return 200, service.apply_command(db, token, payload)
            except HTTPException as exc:
                return exc.status_code, exc.detail

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_send, [create, create]))
    assert results[0] == results[1] and results[0][0] == 200, results
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(m.DiscussionMessage)) == 1
        assert db.scalar(select(func.count()).select_from(m.DiscussionEvent)) == 1
        assert db.get(m.Share, share_id).collaboration_sequence == 1
    print("PASS: full migration, schema parity, downgrade/upgrade, duplicate concurrent sends")

    # Competing edits from the same revision must produce one revision, not two.
    edits = [
        envelope(
            dict(
                type="message.edit",
                thread_id=create.command.thread_id,
                message_id=create.command.message_id,
                expected_revision=1,
                body={"version": 1, "blocks": [{"type": "code", "text": name}]},
            )
        )
        for name in ("first", "second")
    ]
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_send, edits))
    assert sorted(status for status, _ in results) == [200, 409], results
    with Session(engine) as db:
        assert db.get(m.DiscussionMessage, create.command.message_id).revision == 2
        assert db.scalar(select(func.count()).select_from(m.DiscussionMessageRevision)) == 2
        assert db.get(m.Share, share_id).collaboration_sequence == 2
    print("PASS: competing edits serialize; stale command rolls back counter/history/receipt")

    with Session(engine, autoflush=False) as db:
        db.add(
            m.DiscussionReaction(
                message_id=create.command.message_id, user_id=reviewer_id, emoji="eyes"
            )
        )
        db.commit()
        page = service.read_thread(db, token, share_id, entry_id, create.command.thread_id)
        assert page["messages"][0]["reactions"] == [{"emoji": "eyes", "count": 1, "mine": True}]

    # Real SQL locks: each revocation must conflict while a validated command
    # context is held, and becomes possible as soon as the transaction ends.
    guards = [
        select(m.User).where(m.User.id == reviewer_id),
        select(m.UserSession).where(m.UserSession.id == session_id),
        select(m.Share).where(m.Share.id == share_id),
        select(m.ShareMember).where(m.ShareMember.id == member_id),
        select(m.ManagedEntry).where(m.ManagedEntry.id == str(entry_id)),
    ]
    for query in guards:
        with Session(engine, autoflush=False) as holding, Session(engine) as revoker:
            service.context(holding, token, share_id, entry_id, write=True)
            try:
                revoker.scalar(query.with_for_update(nowait=True))
            except OperationalError as exc:
                assert exc.orig.sqlstate == "55P03", exc
                revoker.rollback()
            else:
                raise AssertionError("Missing auth guard")
            holding.rollback()
            assert revoker.scalar(query.with_for_update(nowait=True)) is not None
    print("PASS: user/session/share/membership guards exclude concurrent revocation until commit")

    # Anchor validation is a network hop between two independent authorized SQL
    # transactions. Prove that *real* row locks are released during that hop,
    # and that a committed revocation/generation change defeats the later commit.
    settings = get_settings()
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_text_anchors_enabled = True
    settings.collaboration_anchor_relay_token = SecretStr("disposable-mocked-private-hop")
    for change, expected in [("generation", 409), ("permission", 403), ("withdrawal", 410)]:
        with Session(engine) as db:
            generation = db.get(m.ManagedEntry, str(entry_id)).collaboration_generation
        selected = envelope(
            dict(
                type="thread.create",
                thread_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                anchor=dict(
                    version=1,
                    type="text",
                    document_id=entry_id,
                    generation=generation,
                    dependency="AAA=",
                    start="AA==",
                    end="AA==",
                    selected_length=0,
                    context=dict(quote="", prefix="", suffix="", truncated=False),
                ),
                body=body,
            )
        )
        validating, continue_validation = threading.Event(), threading.Event()

        def private_hop(value):
            validating.set()
            assert continue_validation.wait(5), "SQL locks were not released before validation"
            return anchor_service.AnchorProof(
                anchor_service.command_hash(value),
                entry_id,
                share_id,
                generation,
                anchor_service.now_ms() + 15000,
                time.monotonic() + 12,
                "a" * 64,
                "attached",
            )

        def send_selected():
            with Session(engine, autoflush=False) as db:
                try:
                    return 200, service.apply_command(db, token, selected)
                except HTTPException as exc:
                    return exc.status_code, exc.detail

        with (
            patch.object(anchor_service, "validate", side_effect=private_hop),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(send_selected)
            try:
                assert validating.wait(5), "Private validation did not start"
                with Session(engine) as revoker:
                    for query in guards:
                        assert revoker.scalar(query.with_for_update(nowait=True)) is not None
                    if change == "generation":
                        revoker.get(
                            m.ManagedEntry, str(entry_id)
                        ).collaboration_generation = uuid.uuid4()
                    elif change == "withdrawal":
                        assert (
                            service.withdraw_command(revoker, token, selected)["status"]
                            == "withdrawn"
                        )
                    else:
                        rule = revoker.scalar(
                            select(m.ManagedCommentRule).where(
                                m.ManagedCommentRule.share_id == share_id
                            )
                        )
                        rule.allowed = False
                    revoker.commit()
            finally:
                continue_validation.set()
            assert pending.result(timeout=5)[0] == expected
        with Session(engine) as db:
            assert db.get(m.DiscussionThread, selected.command.thread_id) is None
            receipt = db.get(m.DiscussionReceipt, (reviewer_id, selected.operation_id))
            assert (receipt is not None) == (change == "withdrawal")
            assert db.get(m.Share, share_id).collaboration_sequence == 2
            if change == "permission":
                db.scalar(
                    select(m.ManagedCommentRule).where(m.ManagedCommentRule.share_id == share_id)
                ).allowed = True
                db.commit()
    print(
        "PASS: private validation releases SQL locks; "
        "generation changes, revocation and command withdrawal prevent commit"
    )

    # A stale ORM identity must not resurrect permissions after a committed revoke.
    with Session(engine, autoflush=False) as stale, Session(engine) as revoker:
        stale.get(m.User, reviewer_id)
        stale.get(m.ShareMember, member_id)
        revoker.delete(revoker.get(m.ShareMember, member_id))
        revoker.commit()
        try:
            service.apply_command(stale, token, create)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("Receipt replay bypassed membership revocation")
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(m.DiscussionMessage)) == 1
        assert db.get(m.Share, share_id).collaboration_sequence == 2
    print("PASS: post-revocation receipt replay is denied without writing")

    with Session(engine, autoflush=False) as db:
        user_service.delete_user(db, reviewer_id)
        db.expire_all()
        assert db.get(m.DiscussionMessage, create.command.message_id).author_id is None
        assert db.scalar(select(func.count()).select_from(m.DiscussionMessageRevision)) == 2
        assert db.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 3
        assert db.scalar(select(func.count()).select_from(m.DiscussionReaction)) == 0
        share_service.delete_share(db, db.get(m.Share, share_id))
        assert db.scalar(select(func.count()).select_from(m.DiscussionThread)) == 0
        assert db.scalar(select(func.count()).select_from(m.DiscussionEvent)) == 0
        assert db.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 0
    engine.dispose()
    print("PASS: author deletion preserves discussion history; share deletion cascades cleanly")


if __name__ == "__main__":
    main()

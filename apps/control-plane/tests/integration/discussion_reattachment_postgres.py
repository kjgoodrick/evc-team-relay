"""Real transaction proof; only an empty loopback collaboration_test DB is allowed.

The private relay hop is controlled here to expose SQL races. The real Rust
barrier is exercised separately by managed_stack.py --text-anchors.
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

from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service, session_service
from app.services import discussion_service as service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable loopback collaboration_test database is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as db:
        assert not db.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ), "Expected an empty database"
    m.Base.metadata.create_all(engine)
    settings = get_settings()
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_text_anchors_enabled = True
    settings.collaboration_reattach_enabled = True
    settings.collaboration_anchor_relay_token = SecretStr("disposable-mocked-private-hop")
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@reattach.test", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@reattach.test", password_hash="", is_active=True)
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
                m.ManagedCommentRule(
                    share_id=share.id, user_id=reviewer.id, path="", recursive=True, allowed=True
                ),
            ]
        )
        login, _ = session_service.create_session(db, reviewer.id)
        db.commit()
        share_id, entry_id, actor_id, generation = (
            share.id,
            entry.id,
            reviewer.id,
            entry.collaboration_generation,
        )
        token = security.create_access_token(str(reviewer.id), session_id=str(login.id))

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

    created = envelope(
        dict(
            type="thread.create",
            thread_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            anchor=dict(version=1, type="note"),
            body={
                "version": 1,
                "blocks": [
                    {
                        "type": "paragraph",
                        "children": [{"type": "text", "text": "Move this discussion"}],
                    }
                ],
            },
        )
    )
    with Session(engine, autoflush=False) as db:
        service.apply_command(db, token, created)

    def selection(label):
        return dict(
            version=1,
            type="text",
            document_id=entry_id,
            generation=generation,
            dependency="AAA=",
            start="AA==",
            end="AA==",
            selected_length=0,
            context=dict(quote=label, prefix="", suffix="", truncated=False),
        )

    def move(anchor, revision):
        return envelope(
            dict(
                type="thread.reattach",
                thread_id=created.command.thread_id,
                expected_anchor_revision=revision,
                anchor=anchor,
            )
        )

    def proof(value):
        return anchor_service.AnchorProof(
            anchor_service.command_hash(value),
            uuid.UUID(entry_id),
            share_id,
            generation,
            anchor_service.now_ms() + 15000,
            time.monotonic() + 12,
            "a" * 64,
            "attached",
        )

    def send(payload):
        with Session(engine, autoflush=False) as db:
            try:
                return 200, service.apply_command(db, token, payload)
            except HTTPException as exc:
                return exc.status_code, exc.detail

    barrier = threading.Barrier(2)

    def compete(payload):
        barrier.wait(5)
        return send(payload)

    moves = [move(selection(label), 1) for label in ["first", "second"]]
    with (
        patch.object(anchor_service, "validate", side_effect=proof),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(pool.map(compete, moves))
    assert sorted(result[0] for result in results) == [200, 409], results
    winner = moves[next(i for i, result in enumerate(results) if result[0] == 200)]
    settings.collaboration_reattach_enabled = False
    assert send(winner)[0] == 200
    settings.collaboration_reattach_enabled = True
    with Session(engine) as db:
        assert db.get(m.DiscussionThread, created.command.thread_id).anchor_revision == 2
        assert db.scalar(select(func.count()).select_from(m.DiscussionAnchorRevision)) == 2
    print(
        "PASS: competing moves yield one acceptance and one stale revision; "
        "accepted retry survives disablement"
    )

    rollback = move(dict(version=1, type="note"), 2)
    with Session(engine, autoflush=False) as db:
        with patch.object(db, "commit", side_effect=RuntimeError("injected commit failure")):
            try:
                service.apply_command(db, token, rollback)
            except RuntimeError as exc:
                assert str(exc) == "injected commit failure"
            else:
                raise AssertionError("Expected rollback")
    with Session(engine) as db:
        assert db.get(m.Share, share_id).collaboration_sequence == 2
        assert db.get(m.DiscussionThread, created.command.thread_id).anchor_revision == 2
        for model in [m.DiscussionAnchorRevision, m.DiscussionEvent, m.DiscussionReceipt]:
            assert db.scalar(select(func.count()).select_from(model)) == 2
    print("PASS: failed commit rolls back anchor, counter, event and receipt together")

    for change, expected in [("permission", 403), ("revision", 409), ("withdrawal", 410)]:
        with Session(engine) as db:
            revision = db.get(m.DiscussionThread, created.command.thread_id).anchor_revision
        payload = move(selection("later " + change), revision)
        entered, release = threading.Event(), threading.Event()

        def private_hop(value):
            entered.set()
            assert release.wait(5), "SQL locks remained held during private I/O"
            return proof(value)

        with (
            patch.object(anchor_service, "validate", side_effect=private_hop),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(send, payload)
            try:
                assert entered.wait(5)
                with Session(engine, autoflush=False) as other:
                    assert other.scalar(
                        select(m.Share).where(m.Share.id == share_id).with_for_update(nowait=True)
                    )
                    if change == "permission":
                        other.scalar(
                            select(m.ManagedCommentRule).where(
                                m.ManagedCommentRule.share_id == share_id
                            )
                        ).allowed = False
                        other.commit()
                    elif change == "revision":
                        service.apply_command(
                            other, token, move(dict(version=1, type="note"), revision)
                        )
                    else:
                        assert (
                            service.withdraw_command(other, token, payload)["status"] == "withdrawn"
                        )
            finally:
                release.set()
            assert pending.result(timeout=5)[0] == expected
        with Session(engine) as db:
            receipt = db.get(m.DiscussionReceipt, (actor_id, payload.operation_id))
            assert (receipt is not None) == (change == "withdrawal")
            if change == "permission":
                db.scalar(
                    select(m.ManagedCommentRule).where(m.ManagedCommentRule.share_id == share_id)
                ).allowed = True
                db.commit()
    print(
        "PASS: private I/O releases share locks; "
        "current permission, revision and withdrawal win before commit"
    )
    with Session(engine) as db:
        history = service.anchor_history(db, token, share_id, entry_id, created.command.thread_id)
        assert [row["revision"] for row in history["revisions"]] == [1, 2, 3]
        assert all(row["actor_id"] == str(actor_id) for row in history["revisions"])
        assert history["thread"]["state_revision"] == 1
    engine.dispose()
    print(
        "PASS: immutable target history retains actor/revisions without changing discussion state"
    )


if __name__ == "__main__":
    main()

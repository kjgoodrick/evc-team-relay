"""Migration and replay-boundary proof in an empty disposable loopback PostgreSQL DB."""

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic import command
from alembic.config import Config
from base_discussions_postgres import wait_for_lock
from fastapi import HTTPException
from sqlalchemy import MetaData, Table, create_engine, delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_service as service
from app.services import discussion_snapshot as snapshot
from app.services import session_service


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
        )
    settings = get_settings()
    settings.database_url = url.render_as_string(hide_password=False)
    settings.collaboration_policy_enabled = settings.collaboration_discussions_enabled = True
    settings.collaboration_snapshot_enabled = True
    config = Config("alembic.ini")
    command.upgrade(config, "202611080001")
    with Session(engine) as db:
        owner = m.User(email="owner@event-floor.test", password_hash="", is_active=True)
        db.add(owner)
        db.commit()
        owner_id = owner.id
    share_id = uuid.uuid4()
    old_shares = Table("shares", MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(
            old_shares.insert().values(
                id=share_id,
                kind="folder",
                path="Fixture",
                managed=True,
                visibility="private",
                owner_user_id=owner_id,
            )
        )
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert db.get(m.Share, share_id).discussion_event_floor == 0
    command.downgrade(config, "202611080001")
    command.upgrade(config, "head")
    with Session(engine) as db:
        entry = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=share_id,
            path="Note.md",
            record={"type": "markdown"},
            deleted=False,
        )
        db.add(entry)
        login, _ = session_service.create_session(db, owner_id)
        db.commit()
        entry_id = entry.id
        token = security.create_access_token(str(owner_id), session_id=str(login.id))

    def make_thread(value):
        envelope = CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=dict(
                    type="thread.create",
                    thread_id=uuid.uuid4(),
                    message_id=uuid.uuid4(),
                    anchor=dict(version=1, type="note"),
                    body=dict(
                        version=1,
                        blocks=[dict(type="paragraph", children=[dict(type="text", text=value)])],
                    ),
                ),
            )
        )
        with Session(engine, autoflush=False) as db:
            return service.apply_command(db, token, envelope)

    make_thread("Retained current discussion one")
    with Session(engine, autoflush=False) as db:
        old_cursor = service.changes(db, token, share_id, entry_id)["cursor"]
    make_thread("Retained current discussion two")
    pid = Queue()

    def truncate_owned_fixture():
        with Session(engine) as db:
            pid.put(db.scalar(text("SELECT pg_backend_pid()")))
            db.execute(select(m.Share).where(m.Share.id == share_id).with_for_update())
            db.execute(
                update(m.Share).where(m.Share.id == share_id).values(discussion_event_floor=2)
            )
            db.execute(
                delete(m.DiscussionEvent).where(
                    m.DiscussionEvent.share_id == share_id, m.DiscussionEvent.sequence <= 2
                )
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as executor, Session(engine, autoflush=False) as reader:
        page = service.changes(
            reader, token, share_id, entry_id, cursor=old_cursor, sync_protocol=1
        )
        assert [event["sequence"] for event in page["events"]] == [2]
        future = executor.submit(truncate_owned_fixture)
        try:
            wait_for_lock(engine, pid.get(timeout=5))
            assert not future.done()
        finally:
            reader.commit()
        future.result(timeout=5)
    print(
        "PASS: active change reader holds the retention writer through its response boundary",
        flush=True,
    )

    for protocol, expected in [(None, 426), (1, 409)]:
        with Session(engine, autoflush=False) as db:
            try:
                service.changes(
                    db, token, share_id, entry_id, cursor=old_cursor, sync_protocol=protocol
                )
            except HTTPException as exc:
                assert exc.status_code == expected
            else:
                raise AssertionError("Incomplete retained tail was accepted")
    with Session(engine, autoflush=False) as db:
        baseline = json.loads(snapshot.snapshot(db, token, share_id, entry_id))
        assert len(baseline["threads"]) == 2 and baseline["through_sequence"] == 2
    with Session(engine, autoflush=False) as db:
        assert (
            service.changes(
                db, token, share_id, entry_id, cursor=baseline["cursor"], sync_protocol=1
            )["events"]
            == []
        )
    for invalid in [-1, 3]:
        try:
            with engine.begin() as db:
                db.execute(
                    update(m.Share)
                    .where(m.Share.id == share_id)
                    .values(discussion_event_floor=invalid)
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("Invalid event floor was accepted")
    try:
        command.downgrade(config, "202611080001")
    except RuntimeError as exc:
        assert "replay boundaries" in str(exc)
    else:
        raise AssertionError("A nonzero replay boundary was erased by downgrade")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "202611080002"
        assert db.scalar(select(m.Share.discussion_event_floor).where(m.Share.id == share_id)) == 2
    print(
        "PASS: migration/default/constraints, truncated replay rejection, "
        "complete baseline and protected downgrade",
        flush=True,
    )
    engine.dispose()


if __name__ == "__main__":
    main()

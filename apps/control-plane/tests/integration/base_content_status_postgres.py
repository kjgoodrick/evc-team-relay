"""Read-only Bases receipts and waiting permission checks on a disposable DB."""

import base64
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.db import models as m
from app.schemas.base_content import PublishBaseContent
from app.services import base_content, session_service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only a disposable loopback collaboration_test database is allowed")
    engine = create_engine(url, connect_args={"options": "-c statement_timeout=10000"})
    with engine.connect() as connection:
        if connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
    m.Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@base-status.test", password_hash="", is_active=True)
        writer = m.User(email="writer@base-status.test", password_hash="", is_active=True)
        db.add_all([owner, writer])
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
        entry_id = str(uuid.uuid4())
        entry = m.ManagedEntry(
            id=entry_id,
            share_id=share.id,
            path="Planning.base",
            record={"id": entry_id, "version": 0, "type": "file"},
            deleted=False,
        )
        rule = m.ManagedRule(
            share_id=share.id, user_id=writer.id, path="", recursive=True, permission="write"
        )
        db.add_all(
            [
                entry,
                rule,
                m.ShareMember(share_id=share.id, user_id=writer.id, role=m.ShareMemberRole.VIEWER),
            ]
        )
        session, _ = session_service.create_session(db, writer.id)
        db.commit()
        share_id, rule_id = share.id, rule.id
        token = security.create_access_token(str(writer.id), session_id=str(session.id))
        expected = base_content.read(db, token, share_id, entry_id)["state"]
        payload = PublishBaseContent(
            operation_id=uuid.uuid4(),
            expected=expected,
            content_base64=base64.b64encode(b"views: []\n").decode(),
        )
        accepted = base_content.publish(db, token, share_id, entry_id, payload)
        rule = db.get(m.ManagedRule, rule_id)
        rule.permission = "read"
        db.get(m.Share, share_id).collaboration_restore_generation = uuid.uuid4()
        # The current source may have a different generation or be awaiting
        # repair. Its historical exact receipt remains independently readable.
        db.get(m.ManagedEntry, entry_id).collaboration_generation = uuid.uuid4()
        db.commit()

    writes = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as db:
            assert base_content.command_status(db, token, share_id, entry_id, payload) == {
                "status": "accepted",
                "result": accepted,
            }
            unknown = payload.model_copy(update={"operation_id": uuid.uuid4()})
            assert (
                base_content.command_status(db, token, share_id, entry_id, unknown)["status"]
                == "unknown"
            )
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    pids = Queue()
    with Session(engine) as revoker:
        revoker.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

        def waiting_read():
            with Session(engine, autoflush=False) as db:
                cached = db.get(m.ManagedRule, rule_id)
                assert cached.permission == "read"
                pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                try:
                    base_content.command_status(db, token, share_id, entry_id, payload)
                except HTTPException as error:
                    assert error.status_code == 404
                else:
                    raise AssertionError("A revoked reader received a historical receipt")

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(waiting_read)
            try:
                pid = pids.get(timeout=5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with engine.connect() as probe:
                        locked = (
                            probe.scalar(
                                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                                {"pid": pid},
                            )
                            == "Lock"
                        )
                    if locked:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError("Receipt check did not reach the authority lock")
                revoker.get(m.ManagedRule, rule_id).permission = "none"
                revoker.commit()
            finally:
                revoker.rollback()
            waiting.result(timeout=10)
    engine.dispose()
    print("PASS: historical Bases receipts are read-only; lock-wait revocation prevents disclosure")


if __name__ == "__main__":
    main()

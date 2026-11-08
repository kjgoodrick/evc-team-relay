"""Document mint/socket authority waits on disposable PostgreSQL."""

import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routers import managed
from app.core import security
from app.db import models as m
from app.schemas.token import RelayTokenRequest
from app.services import managed_service as service
from app.services import session_service


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
    with Session(engine) as db:
        owner = m.User(email="owner@socket.test", password_hash="", is_active=True)
        editor = m.User(email="editor@socket.test", password_hash="", is_active=True)
        db.add_all([owner, editor])
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
        rule = m.ManagedRule(
            share_id=share.id, user_id=editor.id, path="", recursive=True, permission="write"
        )
        db.add_all(
            [
                rule,
                m.ShareMember(share_id=share.id, user_id=editor.id, role=m.ShareMemberRole.VIEWER),
                m.ManagedEntry(
                    id=entry_id, share_id=share.id, path="Note.md", record={"type": "markdown"}
                ),
            ]
        )
        login, _ = session_service.create_session(db, editor.id)
        db.commit()
        share_id, user_id, session_id, rule_id = share.id, editor.id, login.id, rule.id
    login_token = security.create_access_token(str(user_id), session_id=str(session_id))

    def request(generation):
        headers = [(b"authorization", ("Bearer " + login_token).encode())]
        if generation:
            headers.append((b"x-relay-restore-generation", str(generation).encode()))
        return Request({"type": "http", "headers": headers})

    for kind, change in [
        ("mint", "restore"),
        ("socket", "restore"),
        ("mint", "permission"),
        ("socket", "permission"),
        ("socket", "session"),
    ]:
        with Session(engine) as setup:
            generation = setup.get(m.Share, share_id).collaboration_restore_generation
            payload = RelayTokenRequest(share_id=share_id, doc_id=entry_id, mode="write")
            grant = service.grant(setup, request(generation), payload, setup.get(m.User, user_id))
            assert not setup.in_transaction(), "Document mint retained its authority lock"
        pids = Queue()

        @contextmanager
        def observed_session():
            with Session(engine, autoflush=False) as db:
                # Preload both authority rows to exercise identity-map refresh.
                db.get(m.Share, share_id)
                db.get(m.User, user_id)
                pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                yield db

        def waiting():
            try:
                if kind == "mint":
                    with observed_session() as db:
                        service.grant(db, request(generation), payload, db.get(m.User, user_id))
                else:
                    with patch.object(managed, "get_sessionmaker", return_value=observed_session):
                        managed.check_socket(grant.token, entry_id)
            except HTTPException as error:
                assert (
                    error.status_code == {"restore": 409, "permission": 403, "session": 401}[change]
                ), error
            else:
                raise AssertionError("Waiting document authority accepted a stale context")

        with Session(engine) as updater:
            updater.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(waiting)
                try:
                    pid = pids.get(timeout=5)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with engine.connect() as probe:
                            locked = (
                                probe.scalar(
                                    text(
                                        "SELECT wait_event_type FROM pg_stat_activity "
                                        "WHERE pid=:pid"
                                    ),
                                    {"pid": pid},
                                )
                                == "Lock"
                            )
                        if locked:
                            break
                        time.sleep(0.01)
                    else:
                        raise AssertionError("Document authority did not reach the share lock")
                    if change == "restore":
                        updater.get(
                            m.Share, share_id
                        ).collaboration_restore_generation = uuid.uuid4()
                    elif change == "permission":
                        updater.get(m.ManagedRule, rule_id).permission = "none"
                    else:
                        updater.delete(updater.get(m.UserSession, session_id))
                    updater.commit()
                finally:
                    updater.rollback()
                future.result(timeout=10)
        if change == "permission":
            with Session(engine) as reset:
                reset.get(m.ManagedRule, rule_id).permission = "write"
                reset.commit()
        print(f"PASS: waiting document {kind} rejects concurrent {change} with fresh authority")
    engine.dispose()


if __name__ == "__main__":
    main()

"""Real PostgreSQL lock waits and unlocked-I/O races for managed attachment writes."""

import asyncio
import hashlib
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.routers import shares
from app.core import security
from app.db import models as m
from app.services import managed_assets, session_service


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
        owner = m.User(email="owner@attachment.test", password_hash="", is_active=True)
        editor = m.User(email="editor@attachment.test", password_hash="", is_active=True)
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
                    id=entry_id, share_id=share.id, path="Image.png", record={"type": "image"}
                ),
            ]
        )
        login, _ = session_service.create_session(db, editor.id)
        db.commit()
        share_id, user_id, session_id, rule_id = share.id, editor.id, login.id, rule.id
    data = b"delayed pre-restore upload"
    sha = hashlib.sha256(data).hexdigest()

    def credential(generation=None):
        token = security.create_file_token(
            subject=str(user_id),
            share_id=str(share_id),
            path=entry_id,
            sha256=sha,
            content_type="image/png",
            content_length=len(data),
            expires_minutes=10,
            session_id=str(session_id),
            restore_generation=str(generation) if generation else None,
        )
        return security.decode_access_token(token)

    payload, pids = credential(), Queue()
    with Session(engine) as restorer:
        restorer.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

        def waiting():
            with Session(engine, autoflush=False) as db:
                stale, user = db.get(m.Share, share_id), db.get(m.User, user_id)
                assert stale.collaboration_restore_generation is None
                pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                try:
                    shares._managed_file_context(db, stale, user, payload, entry_id, write=True)
                except HTTPException as error:
                    assert error.status_code == 409 and error.detail == {
                        "code": "restore_generation_changed"
                    }
                else:
                    raise AssertionError("Waiting upload adopted a restored share")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiting)
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
                    raise AssertionError("Upload did not reach the share lock")
                restorer.get(m.Share, share_id).collaboration_restore_generation = uuid.uuid4()
                restorer.commit()
            finally:
                restorer.rollback()
            future.result(timeout=10)
    print(
        "PASS: an upload waiting behind restore refreshes stale authority and rejects its old token"
    )

    for phase, change in [
        ("body", "restore"),
        ("object", "restore"),
        ("object", "permission"),
        ("object", "session"),
    ]:
        with Session(engine) as setup:
            generation = setup.get(m.Share, share_id).collaboration_restore_generation
        payload = credential(generation)
        objects = {
            managed_assets.object_name(
                share_id, entry_id, hashlib.sha256(b"restored image").hexdigest()
            ): b"restored image"
        }
        original_objects = dict(objects)
        with Session(engine, autoflush=False) as db:

            def change_authority():
                assert not db.in_transaction(), "Attachment I/O retained a SQL transaction"
                with Session(engine) as concurrent:
                    # NOWAIT proves the I/O phase has released its real row lock.
                    current = concurrent.scalar(
                        select(m.Share).where(m.Share.id == share_id).with_for_update(nowait=True)
                    )
                    if change == "restore":
                        current.collaboration_restore_generation = uuid.uuid4()
                    elif change == "permission":
                        concurrent.get(m.ManagedRule, rule_id).permission = "none"
                    else:
                        concurrent.delete(concurrent.get(m.UserSession, session_id))
                    concurrent.commit()

            class Request:
                async def stream(self):
                    if phase == "body":
                        change_authority()
                    yield data

            def put(bucket, key, stream, length, content_type):
                assert phase == "object", "Stale body reached object storage"
                assert stream.read() == data and length == len(data)
                objects[key] = data
                change_authority()

            client = SimpleNamespace(bucket_exists=lambda bucket: True, put_object=put)
            with patch.object(shares, "_get_minio_client", return_value=client):
                try:
                    asyncio.run(
                        shares._put_managed_file(Request(), db, share_id, entry_id, payload)
                    )
                except HTTPException as error:
                    assert (
                        error.status_code
                        == {"restore": 409, "permission": 403, "session": 401}[change]
                    ), error
                else:
                    raise AssertionError("Upload accepted changed authority")
            assert all(objects[key] == value for key, value in original_objects.items())
            assert len(objects) == (1 if phase == "body" else 2)
        if change == "permission":
            with Session(engine) as reset:
                reset.get(m.ManagedRule, rule_id).permission = "write"
                reset.commit()
        print(
            f"PASS: {change} during {phase} I/O proceeds without SQL locks, "
            "rejects success and preserves restored bytes"
        )
    engine.dispose()


if __name__ == "__main__":
    main()

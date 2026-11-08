"""Exercise inventory restore fences with real row-lock waits on disposable PostgreSQL."""

import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import jwt
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.routers.managed import Claim, claim_file
from app.core.config import get_settings
from app.db import models as m
from app.services import managed_service as service


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
        user = m.User(email="owner@inventory-restore.test", password_hash="", is_active=True)
        db.add(user)
        db.flush()
        share = m.Share(
            kind=m.ShareKind.FOLDER,
            path="Fixture",
            managed=True,
            visibility=m.ShareVisibility.PRIVATE,
            owner_user_id=user.id,
        )
        db.add(share)
        db.commit()
        share_id, user_id = share.id, user.id
        record = service.Record(id=uuid.uuid4(), type="markdown")
        original = service.apply_batch(
            db,
            share,
            user,
            service.Batch(
                changes=[service.Change(path="Original.md", previous=None, value=record)]
            ),
        )
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(user_id),
            "scope": "managed",
            "share_id": str(share_id),
            "doc_id": str(share_id),
            "mode": "write",
            "exp": int(time.time()) + 300,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    actions = {
        "noop": lambda db, share, user, old: service.apply_batch(
            db, share, user, service.Batch(), old
        ),
        "move": lambda db, share, user, old: service.apply_batch(
            db,
            share,
            user,
            service.Batch(
                changes=[
                    service.Change(path="Original.md", previous=record, value=None),
                    service.Change(path="Moved.md", previous=None, value=record),
                ]
            ),
            old,
        ),
        "activity": lambda db, share, user, old: service.apply_batch(
            db, share, user, service.Batch(activity={str(record.id): "stale"}), old
        ),
        "existing claim": lambda db, share, user, old: claim_file(
            share_id, Claim(path="Original.md", record=record), db, "Bearer " + token, old
        ),
        "new claim": lambda db, share, user, old: claim_file(
            share_id,
            Claim(path="New.md", record=service.Record(id=uuid.uuid4(), type="markdown")),
            db,
            "Bearer " + token,
            old,
        ),
    }

    def concurrent(action, change, expected):
        pids = Queue()
        with Session(engine) as restorer:
            restorer.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

            def waiting():
                with Session(engine, autoflush=False) as db:
                    share, user = db.get(m.Share, share_id), db.get(m.User, user_id)
                    old = share.collaboration_restore_generation
                    cached = db.get(m.ManagedEntry, str(record.id))
                    assert cached.path == "Original.md"
                    pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                    try:
                        result = action(db, share, user, old)
                    except HTTPException as error:
                        assert expected == "reject" and error.status_code == 409
                        assert error.detail == {"code": "restore_generation_changed"}
                    else:
                        if expected == "reject":
                            raise AssertionError("Waiting inventory mutation bypassed restore")
                        assert result["restore_generation"] == str(expected)
                        assert result["records"] == {"Restored.md": record.wire()}
                        assert not db.in_transaction(), "Manifest retained its authority lock"

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
                        raise AssertionError("Inventory request did not reach the share lock")
                    change(restorer)
                    restorer.commit()
                finally:
                    restorer.rollback()
                future.result(timeout=10)

    for name, action in actions.items():
        concurrent(
            action,
            lambda db: setattr(
                db.get(m.Share, share_id), "collaboration_restore_generation", uuid.uuid4()
            ),
            "reject",
        )
        with Session(engine) as db:
            after = service.manifest(db, db.get(m.Share, share_id), db.get(m.User, user_id))
            for key in ("records", "activity", "deleted"):
                assert after[key] == original[key]
        print(f"PASS: waiting inventory {name} rejects restore before mutation or no-op adoption")
    generation = uuid.uuid4()

    def restore(db):
        db.get(m.Share, share_id).collaboration_restore_generation = generation
        db.get(m.ManagedEntry, str(record.id)).path = "Restored.md"

    concurrent(lambda db, share, user, old: service.manifest(db, share, user), restore, generation)
    engine.dispose()
    print("PASS: waiting manifest refreshes stale share and entry rows and releases its read lock")


if __name__ == "__main__":
    main()

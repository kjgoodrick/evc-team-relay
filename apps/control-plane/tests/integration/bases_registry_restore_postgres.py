"""Restore fencing and read-only saved-view receipts on a disposable PostgreSQL DB."""

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
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import security
from app.db import models as m
from app.schemas.base_content import PublishBaseContent
from app.schemas.bases import InspectBase, ReconcileBase
from app.services import base_content, bases_registry, session_service


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
        owner = m.User(email="owner@registry-restore.test", password_hash="", is_active=True)
        writer = m.User(email="writer@registry-restore.test", password_hash="", is_active=True)
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
        db.add(
            m.ManagedEntry(
                id=entry_id,
                share_id=share.id,
                path="Planning.base",
                record={"id": entry_id, "version": 0, "type": "file"},
                deleted=False,
            )
        )
        rule = m.ManagedRule(
            share_id=share.id, user_id=writer.id, path="", recursive=True, permission="write"
        )
        db.add_all(
            [
                rule,
                m.ShareMember(share_id=share.id, user_id=writer.id, role=m.ShareMemberRole.VIEWER),
            ]
        )
        session, _ = session_service.create_session(db, writer.id)
        db.commit()
        share_id, rule_id = share.id, rule.id
        token = security.create_access_token(str(writer.id), session_id=str(session.id))
        before = base_content.read(db, token, share_id, entry_id)
        content = base_content.publish(
            db,
            token,
            share_id,
            entry_id,
            PublishBaseContent(
                operation_id=uuid.uuid4(),
                expected=before["state"],
                content_base64=base64.b64encode(
                    b"views:\n  - name: Planning\n    type: table\n"
                ).decode(),
            ),
        )
        snapshot = bases_registry.synchronize(
            db,
            token,
            share_id,
            entry_id,
            InspectBase(content_sha256=content["state"]["record"]["hash"]),
        )
        payload = ReconcileBase.model_validate(
            {
                "operation_id": str(uuid.uuid4()),
                "expected_revision": snapshot["revision"],
                "content_sha256": snapshot["content_sha256"],
                "archive": [],
                "bindings": [{"slot": v["slot"], "view_id": v["id"]} for v in snapshot["views"]],
            }
        )
        accepted = bases_registry.synchronize(db, token, share_id, entry_id, payload)
        definition_id = uuid.UUID(accepted["definition_id"])

    def concurrent_change(action, change, expected_status, expected_code=None):
        pids = Queue()
        with Session(engine) as restorer:
            restorer.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

            def waiting():
                with Session(engine, autoflush=False) as db:
                    # Intentionally preload identity-map rows before reaching the
                    # authority lock. The service must refresh them after waiting.
                    old = db.get(m.Share, share_id).collaboration_restore_generation
                    assert db.get(m.ManagedRule, rule_id).permission == "write"
                    pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                    try:
                        action(db, old)
                    except HTTPException as error:
                        assert error.status_code == expected_status
                        if expected_code:
                            assert error.detail == {"code": expected_code}
                    else:
                        raise AssertionError("Waiting request bypassed the concurrent change")

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(waiting)
                try:
                    pid = pids.get(timeout=5)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with engine.connect() as probe:
                            if (
                                probe.scalar(
                                    text(
                                        "SELECT wait_event_type FROM pg_stat_activity "
                                        "WHERE pid=:pid"
                                    ),
                                    {"pid": pid},
                                )
                                == "Lock"
                            ):
                                break
                        time.sleep(0.01)
                    else:
                        raise AssertionError("Request did not reach the authority lock")
                    change(restorer)
                    restorer.commit()
                finally:
                    restorer.rollback()
                future.result(timeout=10)

    for candidate in (
        payload,
        payload.model_copy(
            update={"operation_id": uuid.uuid4(), "expected_revision": accepted["revision"]}
        ),
    ):
        concurrent_change(
            lambda db, old: bases_registry.synchronize(
                db, token, share_id, entry_id, candidate, old
            ),
            lambda db: setattr(
                db.get(m.Share, share_id), "collaboration_restore_generation", uuid.uuid4()
            ),
            409,
            "restore_generation_changed",
        )
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == 2
            assert db.get(m.BaseDefinition, definition_id).revision == accepted["revision"]
    print("PASS: waiting exact replay and new saved-view repair reject a concurrent restore")

    with Session(engine) as db:
        db.get(m.ManagedEntry, entry_id).collaboration_generation = uuid.uuid4()
        db.get(m.ManagedRule, rule_id).permission = "read"
        db.commit()
    writes = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as db:
            assert bases_registry.repair_status(db, token, share_id, entry_id, payload) == {
                "status": "accepted",
                "result": accepted,
            }
            assert (
                bases_registry.repair_status(db, token, share_id, entry_id, candidate)["status"]
                == "unknown"
            )
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    with Session(engine) as db:
        db.get(m.ManagedRule, rule_id).permission = "write"
        db.commit()
    concurrent_change(
        lambda db, _old: bases_registry.repair_status(db, token, share_id, entry_id, payload),
        lambda db: setattr(db.get(m.ManagedRule, rule_id), "permission", "none"),
        404,
    )
    engine.dispose()
    print("PASS: registry receipts issue no writes; waiting read revocation prevents disclosure")


if __name__ == "__main__":
    main()

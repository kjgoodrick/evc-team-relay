"""Disposable PostgreSQL proof for combined file/comment permission editing.

Requires an empty loopback database named collaboration_test. No production DSN.
"""

import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.api.routers.managed import Rule, set_rules
from app.db import models as m
from app.schemas.collaboration import PermissionPolicyUpdate
from app.services import collaboration_policy, permission_editor


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable loopback collaboration_test database is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as conn:
        if conn.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
    m.Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        owner = m.User(email="owner@permissions.test", password_hash="", is_active=True)
        reviewer = m.User(email="reviewer@permissions.test", password_hash="", is_active=True)
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
        entry = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=share.id,
            path="Review/Note.md",
            record={"type": "markdown"},
            deleted=False,
        )
        db.add(entry)
        db.commit()
        share_id, owner_id, reviewer_id, member_id, entry_id = (
            share.id,
            owner.id,
            reviewer.id,
            member.id,
            entry.id,
        )

    def snapshot(db):
        return permission_editor.get_policy(
            db, db.get(m.Share, share_id), db.get(m.User, owner_id), owner_id
        )

    def proposal(current, allow=True):
        return PermissionPolicyUpdate(
            actor_id=owner_id,
            expected_revision=current.revision,
            file_rules=[
                dict(
                    user_id=reviewer_id,
                    path="",
                    recursive=True,
                    permission="read" if allow else "write",
                )
            ],
            comment_rules=[dict(user_id=reviewer_id, path="Review", recursive=True, allowed=allow)],
        )

    with Session(engine) as db:
        observed = snapshot(db)
    barrier = threading.Barrier(2)

    def writer(allow):
        with Session(engine, autoflush=False) as db:
            share, actor = db.get(m.Share, share_id), db.get(m.User, owner_id)
            barrier.wait(timeout=10)
            try:
                result = permission_editor.replace_policy(
                    db, share, actor, proposal(observed, allow)
                )
                return 200, result
            except HTTPException as error:
                db.rollback()
                return error.status_code, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, [True, False]))
    assert sorted(status for status, _ in results) == [200, 409], results
    winner = next(result for status, result in results if status == 200)
    with Session(engine) as db:
        assert snapshot(db) == winner
        caps = collaboration_policy.entry_capabilities(
            db, db.get(m.Share, share_id), db.get(m.User, reviewer_id), entry_id
        )
        assert caps.read and caps.edit != caps.comment
        assert caps.comment == winner.comment_rules[0].allowed
        assert len(list(db.scalars(select(m.AuditLog)))) == 1

    # A fresh combined read refreshes a stale share object and prevents even
    # legacy unversioned rule writers from splitting its two-rule-set snapshot.
    with Session(engine) as reader, Session(engine) as legacy:
        stale = reader.get(m.Share, share_id)
        set_committed_value(stale, "comment_rules_revision", 0)
        current = snapshot(reader)
        assert current == winner
        legacy.execute(text("SET LOCAL lock_timeout = '250ms'"))
        try:
            set_rules(
                share_id,
                [Rule(user_id=reviewer_id, path="", permission="none")],
                legacy,
                legacy.get(m.User, owner_id),
            )
        except OperationalError as error:
            assert error.orig.sqlstate == "55P03", error
            legacy.rollback()
        else:
            raise AssertionError("Combined read did not hold the share policy lock")
        reader.rollback()
        set_rules(
            share_id,
            [Rule(user_id=reviewer_id, path="", permission="none")],
            legacy,
            legacy.get(m.User, owner_id),
        )
        after_legacy = snapshot(legacy)
        assert after_legacy.revision != current.revision
        legacy.rollback()
        try:
            permission_editor.replace_policy(
                reader,
                reader.get(m.Share, share_id),
                reader.get(m.User, owner_id),
                proposal(current),
            )
        except HTTPException as error:
            assert error.status_code == 409
            reader.rollback()
        else:
            raise AssertionError("Stale combined editor overwrote legacy file policy")

    # Failure after both sets and audit have been flushed leaves no partial
    # reviewer grant, revision change or authorization-cache epoch change.
    with Session(engine, autoflush=False) as db:
        before = snapshot(db)
        audit_count = len(list(db.scalars(select(m.AuditLog))))
        with patch.object(db, "commit", side_effect=RuntimeError("Synthetic commit failure")):
            try:
                permission_editor.replace_policy(
                    db, db.get(m.Share, share_id), db.get(m.User, owner_id), proposal(before)
                )
            except RuntimeError:
                db.rollback()
            else:
                raise AssertionError("Commit failure did not propagate")
        assert snapshot(db) == before
        assert len(list(db.scalars(select(m.AuditLog)))) == audit_count

    # A changed target's membership remains stable through the actual commit.
    ready, release = threading.Event(), threading.Event()

    def saving_reviewer():
        with Session(engine, autoflush=False) as db:
            before = snapshot(db)
            db.rollback()
            commit = db.commit

            def hold_commit():
                ready.set()
                assert release.wait(5), "Membership probe did not finish"
                commit()

            with patch.object(db, "commit", side_effect=hold_commit):
                return permission_editor.replace_policy(
                    db, db.get(m.Share, share_id), db.get(m.User, owner_id), proposal(before)
                )

    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(saving_reviewer)
        try:
            assert ready.wait(5), "Permission save did not reach commit"
            with Session(engine) as revoker:
                revoker.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    revoker.delete(revoker.get(m.ShareMember, member_id))
                    revoker.flush()
                except OperationalError as error:
                    assert error.orig.sqlstate == "55P03", error
                    revoker.rollback()
                else:
                    raise AssertionError("Target membership was not held until commit")
        finally:
            release.set()
        saved = saving.result(timeout=10)
    with Session(engine) as db:
        assert snapshot(db) == saved
        caps = collaboration_policy.entry_capabilities(
            db, db.get(m.Share, share_id), db.get(m.User, reviewer_id), entry_id
        )
        assert caps.read and caps.comment and not caps.edit
        db.delete(db.get(m.ShareMember, member_id))
        db.commit()
        try:
            collaboration_policy.entry_capabilities(
                db, db.get(m.Share, share_id), db.get(m.User, reviewer_id), entry_id
            )
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("Removed member retained access")
    # A permission save can have loaded the share before a restore acquired
    # its lock. The post-wait guard must refresh that identity-map object, and
    # run even for an unchanged policy and an otherwise-valid revision.
    with Session(engine) as db:
        before_restore = snapshot(db)
        audit_count = len(list(db.scalars(select(m.AuditLog))))
    old_request = PermissionPolicyUpdate(
        actor_id=owner_id,
        expected_revision=before_restore.revision,
        file_rules=before_restore.file_rules,
        comment_rules=before_restore.comment_rules,
    )
    generation = uuid.uuid4()
    pids = Queue()
    with Session(engine) as restorer:
        restorer.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

        def waiting_save():
            with Session(engine, autoflush=False) as db:
                cached = db.get(m.Share, share_id)
                assert cached.collaboration_restore_generation is None
                actor = db.get(m.User, owner_id)
                pids.put(db.scalar(text("SELECT pg_backend_pid()")))
                try:
                    permission_editor.replace_policy(db, cached, actor, old_request)
                except HTTPException as error:
                    assert error.status_code == 409
                    assert error.detail == {"code": "restore_generation_changed"}
                else:
                    raise AssertionError("An old permission form bypassed the restore guard")

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(waiting_save)
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
                    raise AssertionError("Permission save did not reach the share lock")
                restorer.get(m.Share, share_id).collaboration_restore_generation = generation
                restorer.commit()
            finally:
                restorer.rollback()
            waiting.result(timeout=10)
    with Session(engine) as db:
        restored = snapshot(db)
        assert restored.restore_generation == generation and restored.restore_guard
        assert restored.revision != before_restore.revision
        assert restored.file_rules == before_restore.file_rules
        assert restored.comment_rules == before_restore.comment_rules
        assert len(list(db.scalars(select(m.AuditLog)))) == audit_count
        fresh = old_request.model_copy(update={"expected_revision": restored.revision})
        assert (
            permission_editor.replace_policy(
                db, db.get(m.Share, share_id), db.get(m.User, owner_id), fresh, generation
            )
            == restored
        )
    print("PASS: waiting permission saves reject restore; fresh reviewed no-op remains valid")
    engine.dispose()
    print(
        "PASS: combined policy 200/409 concurrency, coherent reads, "
        "legacy invalidation, rollback and membership guards"
    )


if __name__ == "__main__":
    main()

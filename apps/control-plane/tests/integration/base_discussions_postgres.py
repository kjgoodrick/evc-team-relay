"""Bases discussion identity/filtering/authorization against disposable PostgreSQL.

Requires an empty localhost collaboration_test database. No object store,
existing service, vault or production account is used. Configuration bytes go
through the real guarded publication and registry inspection services.
"""

import base64
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from base_groups_postgres import prove_groups
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from timeline_markers_postgres import prove_timeline_markers
from timeline_ranges_postgres import prove_timeline_ranges

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.base_content import PublishBaseContent
from app.schemas.bases import BaseTargetCatalog, BaseTargetContext, InspectBase
from app.schemas.discussions import CommandEnvelope
from app.services import (
    base_content,
    base_discussions,
    base_target_definitions,
    bases_registry,
    managed_service,
    session_service,
)
from app.services import discussion_service as discussions


def wait_for_lock(engine, pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            if (
                connection.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                == "Lock"
            ):
                return
        time.sleep(0.01)
    raise AssertionError("The competing request did not reach its share-lock wait")


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable localhost collaboration_test database is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as connection:
        if connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
    settings = get_settings()
    settings.database_url = url.render_as_string(hide_password=False)
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_bases_registry_enabled = True
    settings.collaboration_bases_discussions_enabled = True
    settings.managed_bases_sync_enabled = True
    command.upgrade(Config("alembic.ini"), "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070002"
        drift = compare_metadata(MigrationContext.configure(connection), m.Base.metadata)
        assert not [
            item
            for item in drift
            if any(
                name in str(item)
                for name in ("collaboration_base_group", "collaboration_timeline_marker")
            )
        ], drift
    command.downgrade(Config("alembic.ini"), "202611060005")
    command.upgrade(Config("alembic.ini"), "head")
    print("PASS: group migration matches ORM metadata and empty-table downgrade/upgrade succeeds")
    users, share_id = [], None
    try:
        with Session(engine, autoflush=False) as db:
            owner = m.User(email="owner@base-discussions.test", password_hash="", is_active=True)
            reviewer = m.User(
                email="reviewer@base-discussions.test", password_hash="", is_active=True
            )
            db.add_all([owner, reviewer])
            db.flush()
            users = [owner.id, reviewer.id]
            share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Fixture",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=owner.id,
            )
            db.add(share)
            db.flush()
            share_id, owner_id, reviewer_id = share.id, owner.id, reviewer.id
            db.add(
                m.ShareMember(share_id=share_id, user_id=reviewer_id, role=m.ShareMemberRole.VIEWER)
            )
            db.add(
                m.ManagedRule(
                    share_id=share_id,
                    user_id=reviewer_id,
                    path="Notes",
                    recursive=True,
                    permission="read",
                )
            )
            db.add(
                m.ManagedCommentRule(
                    share_id=share_id,
                    user_id=reviewer_id,
                    path="Notes",
                    recursive=True,
                    allowed=True,
                )
            )
            entry_id, record_id = uuid.uuid4(), uuid.uuid4()
            for identity, path, kind in [
                (entry_id, "Notes/Planning.base", "file"),
                (record_id, "Notes/Record.md", "markdown"),
            ]:
                db.add(
                    m.ManagedEntry(
                        id=str(identity),
                        share_id=share_id,
                        path=path,
                        deleted=False,
                        record={"id": str(identity), "version": 0, "type": kind},
                    )
                )
            tokens = {}
            for name, user in [("owner", owner), ("reviewer", reviewer)]:
                session, _ = session_service.create_session(db, user.id)
                tokens[name] = security.create_access_token(
                    str(user.id), session_id=str(session.id)
                )
            db.commit()
        with Session(engine, autoflush=False) as db:
            current = base_content.read(db, tokens["owner"], share_id, entry_id)
            saved = base_content.publish(
                db,
                tokens["owner"],
                share_id,
                entry_id,
                PublishBaseContent.model_validate(
                    {
                        "operation_id": str(uuid.uuid4()),
                        "expected": current["state"],
                        "content_base64": base64.b64encode(
                            b"views:\n  - type: table\n    name: Table\n"
                            b"  - type: cards\n    name: Cards\n"
                        ).decode(),
                    }
                ),
            )
            registry = bases_registry.synchronize(
                db,
                tokens["reviewer"],
                share_id,
                entry_id,
                InspectBase(content_sha256=saved["state"]["record"]["hash"]),
            )
        view_id, second_view = [uuid.UUID(view["id"]) for view in registry["views"]]
        barrier = threading.Barrier(2)

        def capture():
            with Session(engine, autoflush=False) as db:
                barrier.wait(timeout=5)
                return base_discussions.target_context(
                    db,
                    tokens["reviewer"],
                    share_id,
                    entry_id,
                    BaseTargetContext(view_id=view_id, record_id=record_id),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(capture), pool.submit(capture)
            captured = a.result()
            assert captured == b.result()
        print(
            "PASS: concurrent reader captures initialize one record generation without file edits"
        )

        def envelope(view=view_id):
            anchor = {
                key: captured[key] for key in ("definition_id", "generation", "registry_revision")
            }
            anchor.update(
                version=1,
                type="base",
                view_id=str(view),
                target={
                    "type": "cell",
                    "record_id": str(record_id),
                    "record_generation": captured["record_generation"],
                    "property": {"source": "note", "name": "status"},
                },
            )
            return CommandEnvelope.model_validate(
                {
                    "protocol_version": 1,
                    "operation_id": str(uuid.uuid4()),
                    "share_id": str(share_id),
                    "entry_id": str(entry_id),
                    "command": {
                        "type": "thread.create",
                        "thread_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "anchor": anchor,
                        "body": {"version": 1, "blocks": [{"type": "code", "text": "Review"}]},
                    },
                }
            )

        original = envelope()
        barrier = threading.Barrier(2)

        def send():
            with Session(engine, autoflush=False) as db:
                barrier.wait(timeout=5)
                return discussions.apply_command(db, tokens["reviewer"], original)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(send), pool.submit(send)
            first = a.result()
            assert first == b.result()
        with Session(engine, autoflush=False) as db:
            second = discussions.apply_command(db, tokens["reviewer"], envelope(second_view))
            page = discussions.list_threads(
                db, tokens["reviewer"], share_id, entry_id, view_id=view_id
            )
            assert [item["id"] for item in page["threads"]] == [first["thread_id"]]
            other = discussions.changes(
                db, tokens["reviewer"], share_id, entry_id, view_id=second_view
            )
            assert [event["data"]["thread_id"] for event in other["events"]] == [
                second["thread_id"]
            ]
        print(
            "PASS: concurrent exact retries commit once; "
            "PostgreSQL filters lists/events by saved view"
        )

        # Prime ORM objects before revocation; the service must refresh after its
        # share-lock wait, not rely on these previously readable identities.
        for mutation in [True, False]:
            pending = envelope()
            pid_queue = Queue()
            with Session(engine, autoflush=False) as stale, Session(engine) as revoker:
                stale.get(m.Share, share_id)
                stale.get(m.ManagedEntry, str(record_id))
                stale.commit()
                revoker.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

                def blocked_request():
                    pid_queue.put(stale.scalar(text("SELECT pg_backend_pid()")))
                    try:
                        if mutation:
                            discussions.apply_command(stale, tokens["reviewer"], pending)
                        else:
                            discussions.read_thread(
                                stale,
                                tokens["reviewer"],
                                share_id,
                                entry_id,
                                original.command.thread_id,
                            )
                    except HTTPException as exc:
                        return exc.status_code
                    return 200

                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(blocked_request)
                    try:
                        wait_for_lock(engine, pid_queue.get(timeout=5))
                        rule = m.ManagedRule(
                            share_id=share_id,
                            user_id=reviewer_id,
                            path="Notes/Record.md",
                            recursive=False,
                            permission="none",
                        )
                        revoker.add(rule)
                        managed_service.invalidate_discussion_snapshots(revoker, share_id)
                        revoker.commit()
                    finally:
                        revoker.rollback()  # Release the lock even if the assertion fails.
                    assert future.result(timeout=5) == 404
                assert stale.get(m.DiscussionThread, pending.command.thread_id) is None
            with Session(engine) as db:
                db.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
                rule = db.scalar(
                    select(m.ManagedRule).where(
                        m.ManagedRule.share_id == share_id, m.ManagedRule.path == "Notes/Record.md"
                    )
                )
                db.delete(rule)
                managed_service.invalidate_discussion_snapshots(db, share_id)
                db.commit()
        print("PASS: reads and writes waiting behind record revocation reject stale ORM authority")

        with Session(engine, autoflush=False) as db:
            cursor = discussions.changes(
                db, tokens["reviewer"], share_id, entry_id, view_id=view_id
            )["cursor"]
            record = db.get(m.ManagedEntry, str(record_id))
            managed_service.apply_batch(
                db,
                db.get(m.Share, share_id),
                db.get(m.User, owner_id),
                managed_service.Batch(
                    changes=[
                        {"path": record.path, "previous": record.record, "value": None},
                        {"path": "Private/Moved.md", "previous": None, "value": record.record},
                    ]
                ),
            )
            try:
                discussions.changes(
                    db, tokens["reviewer"], share_id, entry_id, view_id=view_id, cursor=cursor
                )
            except HTTPException as exc:
                assert exc.status_code == 409 and exc.detail["code"] == "snapshot_required"
            else:
                raise AssertionError("Moved target retained an obsolete cursor")
            db.rollback()
            assert (
                discussions.list_threads(
                    db, tokens["reviewer"], share_id, entry_id, view_id=view_id
                )["threads"]
                == []
            )
            assert db.scalar(select(func.count()).select_from(m.DiscussionThread)) == 2
        print(
            "PASS: a move under an unreadable parent resets the cursor and hides existing targets"
        )

        settings.collaboration_bases_definition_targets_enabled = True
        settings.collaboration_bases_formula_cells_enabled = True
        # Return the record to the comment-only reviewer's authorized folder.
        with Session(engine, autoflush=False) as db:
            record = db.get(m.ManagedEntry, str(record_id))
            managed_service.apply_batch(
                db,
                db.get(m.Share, share_id),
                db.get(m.User, owner_id),
                managed_service.Batch(
                    changes=[
                        {"path": record.path, "previous": record.record, "value": None},
                        {"path": "Notes/Record.md", "previous": None, "value": record.record},
                    ]
                ),
            )

        def publish_definitions(expression):
            raw = json.dumps(
                {
                    "formulas": {"total": expression},
                    "views": [
                        {
                            "name": "Table",
                            "type": "table",
                            "summaries": {"formula.total": "Average"},
                        },
                        {"name": "Cards", "type": "cards"},
                    ],
                }
            ).encode()
            with Session(engine, autoflush=False) as db:
                current = base_content.read(db, tokens["owner"], share_id, entry_id)
                saved = base_content.publish(
                    db,
                    tokens["owner"],
                    share_id,
                    entry_id,
                    PublishBaseContent.model_validate(
                        {
                            "operation_id": str(uuid.uuid4()),
                            "expected": current["state"],
                            "content_base64": base64.b64encode(raw).decode(),
                        }
                    ),
                )
                bases_registry.synchronize(
                    db,
                    tokens["owner"],
                    share_id,
                    entry_id,
                    InspectBase(content_sha256=saved["state"]["record"]["hash"]),
                )
                return base_target_definitions.catalog(
                    db, tokens["reviewer"], share_id, entry_id, BaseTargetCatalog(view_id=view_id)
                )

        definitions = publish_definitions("price * 2")

        def defined_command(kind):
            target = next(
                item
                for item in definitions["targets"]
                if item["kind"] == ("formula" if kind == "formula_cell" else kind)
            )
            value = envelope().model_dump(mode="json")
            value["command"]["anchor"] = {
                "version": 1,
                "type": "base",
                **{
                    key: definitions[key]
                    for key in ("definition_id", "view_id", "generation", "registry_revision")
                },
                "target": {
                    "type": kind,
                    "definition_id": target["id"],
                    "definition_revision": target["revision"],
                    "property": target["property"],
                    **({"summary": target["summary"]} if kind == "summary" else {}),
                    **(
                        {
                            "record_id": str(record_id),
                            "record_generation": captured["record_generation"],
                        }
                        if kind == "formula_cell"
                        else {}
                    ),
                },
            }
            return CommandEnvelope.model_validate(value)

        created = []
        kinds = ("formula", "summary", "formula_cell")
        for kind in kinds:
            payload = defined_command(kind)
            barrier = threading.Barrier(2)

            def send_defined():
                with Session(engine, autoflush=False) as db:
                    barrier.wait(timeout=5)
                    return discussions.apply_command(db, tokens["reviewer"], payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(send_defined), pool.submit(send_defined)
                result = first.result()
                assert second.result() == result
            created.append((payload, result))

        with Session(engine, autoflush=False, expire_on_commit=False) as stale:
            cached_definitions = [
                stale.get(m.BaseTargetDefinition, uuid.UUID(item["id"]))
                for item in definitions["targets"]
            ]
            stale.commit()
            publish_definitions("price * 3")
            for kind, (payload, accepted) in zip(kinds, created):
                try:
                    discussions.apply_command(stale, tokens["reviewer"], defined_command(kind))
                except HTTPException as exc:
                    assert (
                        exc.status_code == 409
                        and exc.detail["code"] == "base_target_definition_changed"
                    )
                else:
                    raise AssertionError("A stale ORM definition authorized new capture")
                assert discussions.apply_command(stale, tokens["reviewer"], payload) == accepted
                assert (
                    discussions.read_thread(
                        stale, tokens["reviewer"], share_id, entry_id, payload.command.thread_id
                    )["anchor"]["target"]["definition_revision"]
                    == 2
                )
            assert stale.scalar(select(func.count()).select_from(m.DiscussionThread)) == 5
        print(
            "PASS: formula/summary/formula-cell retries serialize once; definition edits reject "
            "stale ORM capture without changing historical access or accepted receipts"
        )
        prove_groups(engine, tokens, share_id, entry_id, record_id, view_id, reviewer_id)
        prove_timeline_ranges(
            engine, tokens, share_id, entry_id, record_id, view_id, reviewer_id, wait_for_lock
        )
        prove_timeline_markers(
            engine, tokens, share_id, entry_id, view_id, reviewer_id, wait_for_lock
        )
    finally:
        with Session(engine) as db:
            if share_id is not None and (share := db.get(m.Share, share_id)):
                db.delete(share)
            for user_id in users:
                if user := db.get(m.User, user_id):
                    db.delete(user)
            db.commit()
            assert db.scalar(select(func.count()).select_from(m.DiscussionThread)) == 0
            assert db.scalar(select(func.count()).select_from(m.BaseDefinition)) == 0
            for model in (
                m.BaseGroupValue,
                m.BaseGroupWitness,
                m.BaseGroupCaptureReceipt,
                m.BaseTimelineMarkerCatalog,
                m.BaseTimelineMarker,
                m.BaseTimelineMarkerPlacement,
                m.BaseTimelineMarkerRevision,
                m.BaseTimelineMarkerReceipt,
            ):
                assert db.scalar(select(func.count()).select_from(model)) == 0
        engine.dispose()
    print("PASS: synthetic accounts/share and their discussion/registry data removed")


if __name__ == "__main__":
    main()

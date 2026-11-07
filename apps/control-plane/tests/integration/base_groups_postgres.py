"""Group capture races, witness isolation and migration guards on PostgreSQL.

Called from base_discussions_postgres.py with its disposable fixture. Private
source observation is controlled here; managed_stack.py exercises the real relay.
"""

import base64
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models as m
from app.schemas.base_content import PublishBaseContent
from app.schemas.bases import BaseTargetCatalog, BaseTargetContext, CaptureBaseGroup, InspectBase
from app.schemas.discussions import CommandEnvelope
from app.services import (
    anchor_service,
    base_content,
    base_discussions,
    base_groups,
    base_source_service,
    base_target_definitions,
    bases_registry,
    discussion_service,
    managed_service,
)


def prove_groups(engine, tokens, share_id, entry_id, record_id, view_id, reviewer_id):
    settings = get_settings()
    settings.collaboration_bases_groups_enabled = True
    settings.collaboration_bases_group_summaries_enabled = True
    settings.collaboration_anchor_relay_token = SecretStr("synthetic-observation-only")

    def publish(group=True, property_name="status"):
        raw = json.dumps(
            {
                "views": [
                    {
                        "name": "Table",
                        "type": "table",
                        **({"groupBy": {"property": property_name}} if group else {}),
                        "summaries": {"formula.total": "Average"},
                    },
                    {"name": "Cards", "type": "cards"},
                ],
                "formulas": {"total": "2"},
            }
        ).encode()
        with Session(engine, autoflush=False) as db:
            current = base_content.read(db, tokens["owner"], share_id, entry_id)
            saved = base_content.publish(
                db,
                tokens["owner"],
                share_id,
                entry_id,
                PublishBaseContent(
                    operation_id=uuid.uuid4(),
                    expected=current["state"],
                    content_base64=base64.b64encode(raw).decode(),
                ),
            )
            bases_registry.synchronize(
                db,
                tokens["owner"],
                share_id,
                entry_id,
                InspectBase(content_sha256=saved["state"]["record"]["hash"]),
            )

    def request(record=record_id):
        with Session(engine, autoflush=False) as db:
            captured = base_discussions.target_context(
                db,
                tokens["reviewer"],
                share_id,
                entry_id,
                BaseTargetContext(view_id=view_id, record_id=record),
            )
            page = base_target_definitions.catalog(
                db, tokens["reviewer"], share_id, entry_id, BaseTargetCatalog(view_id=view_id)
            )
            group = next(item for item in page["targets"] if item["kind"] == "group")
        return CaptureBaseGroup.model_validate(
            {
                **{
                    key: captured[key]
                    for key in (
                        "definition_id",
                        "view_id",
                        "generation",
                        "registry_revision",
                        "record_id",
                        "record_generation",
                    )
                },
                "operation_id": str(uuid.uuid4()),
                "dependency": "AA==",
                "time_zone": "UTC",
                "group_definition_id": group["id"],
                "group_definition_revision": group["revision"],
                "value": {"type": "text", "value": "Done"},
            }
        )

    publish()
    thread_state = threading.local()
    real_context = discussion_service.context
    barrier, entered, release = None, None, None
    file_boundary = False

    def track(db, *args, **kwargs):
        thread_state.db = db
        if file_boundary:
            thread_state.context_calls = getattr(thread_state, "context_calls", 0) + 1
            if thread_state.context_calls == 2:
                assert not db.in_transaction()
                entered.set()
                assert release.wait(timeout=10)
        return real_context(db, *args, **kwargs)

    def observe(share, record, generation, dependency):
        assert not thread_state.db.in_transaction(), "Source RPC retained a SQL transaction"
        if barrier:
            barrier.wait(timeout=10)
        else:
            # During duplicate capture the other legitimate request may briefly
            # hold this lock. Probe NOWAIT only when this is the sole requester;
            # each request's own transaction state is checked above in both cases.
            with Session(engine) as guard:
                guard.execute(
                    select(m.Share.id).where(m.Share.id == share_id).with_for_update(nowait=True)
                )
        if entered:
            entered.set()
            assert release.wait(timeout=10), "Source observation was never released"
        source = "---\nstatus: Done\n---\nSynthetic content"
        return base_source_service.SourceObservation(
            base_source_service.source_identity(share, record, generation, dependency),
            hashlib.sha256(dependency.encode()).hexdigest(),
            hashlib.sha256(source.encode()).hexdigest(),
            True,
            anchor_service.now_ms() + 15000,
            time.monotonic() + 12,
            source,
        )

    def capture(payload):
        with Session(engine, autoflush=False) as db:
            return base_groups.capture(db, tokens["reviewer"], share_id, entry_id, payload)

    def totals():
        with Session(engine) as db:
            return tuple(
                db.scalar(select(func.count()).select_from(model))
                for model in (m.BaseGroupValue, m.BaseGroupWitness, m.BaseGroupCaptureReceipt)
            )

    def create_thread(captured, summary=False):
        anchor = captured["anchor"]
        if summary:
            with Session(engine, autoflush=False) as db:
                page = base_target_definitions.catalog(
                    db, tokens["reviewer"], share_id, entry_id, BaseTargetCatalog(view_id=view_id)
                )
            definition = next(item for item in page["targets"] if item["kind"] == "summary")
            anchor = {
                **anchor,
                "target": {
                    "type": "group_summary",
                    "definition_id": definition["id"],
                    "definition_revision": definition["revision"],
                    "property": definition["property"],
                    "summary": definition["summary"],
                    "group": anchor["target"],
                },
            }
        payload = CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=dict(
                    type="thread.create",
                    thread_id=uuid.uuid4(),
                    message_id=uuid.uuid4(),
                    anchor=anchor,
                    body={"version": 1, "blocks": [{"type": "code", "text": "Group review"}]},
                ),
            )
        )
        with Session(engine, autoflush=False) as db:
            return discussion_service.apply_command(db, tokens["reviewer"], payload)

    with (
        patch.object(discussion_service, "context", track),
        patch.object(base_source_service, "observe", observe),
    ):
        payload = request()
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(capture, payload), pool.submit(capture, payload)
            captured = a.result(timeout=15)
            assert b.result(timeout=15) == captured
        barrier = None
        assert totals() == (1, 1, 1)
        protected = create_thread(captured)
        protected_summary = create_thread(captured, summary=True)
        # Different operations with the same source reuse immutable evidence.
        assert capture(request())["anchor"]["target"] == captured["anchor"]["target"]
        assert totals() == (1, 1, 2)
        print("PASS: concurrent group capture commits one value, witness and exact receipt")

        other_record = uuid.uuid4()
        with Session(engine) as db:
            db.add(
                m.ManagedEntry(
                    id=str(other_record),
                    share_id=share_id,
                    path="Notes/Visible.md",
                    record={"id": str(other_record), "type": "markdown", "version": 0},
                    deleted=False,
                )
            )
            db.commit()
        other = capture(request(other_record))
        assert other["anchor"]["target"]["group_id"] == captured["anchor"]["target"]["group_id"]
        assert other["anchor"]["target"]["witness_id"] != captured["anchor"]["target"]["witness_id"]
        accessible = create_thread(other)
        accessible_summary = create_thread(other, summary=True)
        with Session(engine) as db:
            source_path = db.get(m.ManagedEntry, str(record_id)).path
            db.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
            rule = m.ManagedRule(
                share_id=share_id,
                user_id=reviewer_id,
                path=source_path,
                recursive=False,
                permission="none",
            )
            db.add(rule)
            managed_service.invalidate_discussion_snapshots(db, share_id)
            db.flush()
            rule_id = rule.id
            db.commit()
        with Session(engine, autoflush=False) as db:
            visible = discussion_service.list_threads(
                db, tokens["reviewer"], share_id, entry_id, view_id=view_id
            )
            ids = {thread["id"] for thread in visible["threads"]}
            assert protected["thread_id"] not in ids and accessible["thread_id"] in ids
            assert (
                protected_summary["thread_id"] not in ids and accessible_summary["thread_id"] in ids
            )
            for hidden in (protected, protected_summary):
                try:
                    discussion_service.read_thread(
                        db, tokens["reviewer"], share_id, entry_id, uuid.UUID(hidden["thread_id"])
                    )
                except HTTPException as exc:
                    assert exc.status_code == 404
                else:
                    raise AssertionError("Matching accessible group unlocked a protected witness")
        with Session(engine) as db:
            db.delete(db.get(m.ManagedRule, rule_id))
            managed_service.invalidate_discussion_snapshots(db, share_id)
            db.commit()
        print("PASS: groups and group summaries cannot substitute a matching readable witness")

        for change in ("permission", "generation", "definition"):
            pending = request()
            before = totals()
            entered, release = threading.Event(), threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(capture, pending)
                try:
                    assert entered.wait(timeout=10), "Capture never reached private I/O"
                    if change == "definition":
                        publish(False)
                    else:
                        with Session(engine) as revoker:
                            revoker.execute(
                                select(m.Share.id)
                                .where(m.Share.id == share_id)
                                .with_for_update(nowait=True)
                            )
                            if change == "permission":
                                rule = m.ManagedRule(
                                    share_id=share_id,
                                    user_id=reviewer_id,
                                    path=source_path,
                                    recursive=False,
                                    permission="none",
                                )
                                revoker.add(rule)
                                revoker.flush()
                                rule_id = rule.id
                            else:
                                revoker.get(
                                    m.ManagedEntry, str(record_id)
                                ).collaboration_generation = uuid.uuid4()
                            managed_service.invalidate_discussion_snapshots(revoker, share_id)
                            revoker.commit()
                finally:
                    release.set()
                try:
                    future.result(timeout=10)
                except HTTPException as exc:
                    assert exc.status_code == (404 if change == "permission" else 409), exc.detail
                else:
                    raise AssertionError(f"Capture ignored a concurrent {change} change")
            entered, release = None, None
            assert totals() == before
            if change == "permission":
                with Session(engine) as db:
                    db.delete(db.get(m.ManagedRule, rule_id))
                    managed_service.invalidate_discussion_snapshots(db, share_id)
                    db.commit()
        print(
            "PASS: source revocation, lineage changes and definition removal "
            "during RPC prevent commit"
        )

        # Data-bearing migrations must not silently discard target evidence.
        try:
            command.downgrade(Config("alembic.ini"), "202611060005")
        except RuntimeError as exc:
            assert "Export/migrate group targets" in str(exc)
        else:
            raise AssertionError("Populated group tables were downgraded")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070002"
            )
        assert totals() == (1, 2, 3)
        print(
            "PASS: populated group migration refuses downgrade "
            "without losing values, witnesses or receipts"
        )

        publish(property_name="file.ext")

        def file_request():
            payload = request().model_dump(mode="json", exclude_none=True)
            payload.pop("dependency")
            payload.pop("time_zone")
            with Session(engine) as db:
                path = db.get(m.ManagedEntry, str(record_id)).path
            payload.update(
                source_kind="file",
                source_path_sha256=hashlib.sha256(path.encode()).hexdigest(),
                value={"type": "text", "value": path.rsplit(".", 1)[-1]},
            )
            return CaptureBaseGroup.model_validate(payload)

        before = totals()
        file_payload = file_request()
        with patch.object(
            base_source_service,
            "observe",
            side_effect=AssertionError("File groups must not read a CRDT"),
        ):
            start = threading.Barrier(2)

            def simultaneous():
                start.wait(timeout=10)
                return capture(file_payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                left, right = pool.submit(simultaneous), pool.submit(simultaneous)
                assert left.result(timeout=15) == right.result(timeout=15)
        assert totals() == tuple(count + 1 for count in before)
        print("PASS: concurrent inventory group capture commits one exact receipt without note I/O")

        # Pause precisely between the two authoritative reads. A capture must
        # recheck a rename, lineage change or revocation committed in that gap.
        for change in ("path", "generation", "permission"):
            pending = file_request()
            before = totals()
            entered, release = threading.Event(), threading.Event()
            file_boundary = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(capture, pending)
                try:
                    assert entered.wait(timeout=10)
                    with Session(engine) as db:
                        db.execute(
                            select(m.Share.id)
                            .where(m.Share.id == share_id)
                            .with_for_update(nowait=True)
                        )
                        record = db.get(m.ManagedEntry, str(record_id))
                        if change == "path":
                            record.path = "Notes/Inventory renamed.md"
                        elif change == "generation":
                            record.collaboration_generation = uuid.uuid4()
                        else:
                            rule = m.ManagedRule(
                                share_id=share_id,
                                user_id=reviewer_id,
                                path=record.path,
                                recursive=False,
                                permission="none",
                            )
                            db.add(rule)
                            db.flush()
                            rule_id = rule.id
                        db.commit()
                finally:
                    release.set()
                try:
                    future.result(timeout=10)
                except HTTPException as exc:
                    assert exc.status_code == (404 if change == "permission" else 409), exc.detail
                else:
                    raise AssertionError(f"File capture ignored concurrent {change}")
            file_boundary = False
            entered, release = None, None
            assert totals() == before
            if change == "permission":
                with Session(engine) as db:
                    db.delete(db.get(m.ManagedRule, rule_id))
                    db.commit()
        print(
            "PASS: inventory source rename, lineage change and revocation "
            "prevent stale capture commit"
        )

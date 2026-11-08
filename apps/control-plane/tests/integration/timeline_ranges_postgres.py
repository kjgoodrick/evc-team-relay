"""Timeline range transactions on the disposable Bases PostgreSQL fixture.

Called by base_discussions_postgres.py after its group checks. Publication,
registry inspection, axis/group capture, commands and reads use real services.
Only commit failures are injected; races wait for an observed PostgreSQL lock.
"""

import base64
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from alembic import command as migrations
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models as m
from app.schemas.base_content import PublishBaseContent
from app.schemas.bases import (
    BaseTargetCatalog,
    BaseTargetContext,
    BaseTimelineAxisContext,
    CaptureBaseGroup,
    InspectBase,
)
from app.schemas.discussions import CommandEnvelope
from app.services import (
    base_content,
    base_discussions,
    base_groups,
    base_target_definitions,
    bases_registry,
    managed_service,
    timeline_axes,
)
from app.services import discussion_service as discussions

RANGES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/timeline-ranges-v1.json").read_text()
)["valid"]


def prove_timeline_ranges(
    engine, tokens, share_id, entry_id, record_id, view_id, reviewer_id, wait_for_lock
):
    settings = get_settings()
    settings.collaboration_bases_timeline_ranges_enabled = True

    def content(*, sequence=False, property_name=None, grouped=False):
        return json.dumps(
            {
                "views": [
                    {
                        "name": "Table",
                        "type": "sabidurian-sequence" if sequence else "sabidurian-timeline",
                        **(
                            {"orderProp": property_name or "note.order"}
                            if sequence
                            else {
                                "startDateProp": property_name or "note.start",
                                "timeZone": "America/Denver",
                            }
                        ),
                        **({"groupBy": {"property": "file.ext"}} if grouped else {}),
                    },
                    {"name": "Cards", "type": "cards"},
                ]
            }
        ).encode()

    def publish_bytes(db, raw, expected):
        saved = base_content.publish(
            db,
            tokens["owner"],
            share_id,
            entry_id,
            PublishBaseContent(
                operation_id=uuid.uuid4(),
                expected=expected,
                content_base64=base64.b64encode(raw).decode(),
            ),
        )
        return bases_registry.synchronize(
            db,
            tokens["owner"],
            share_id,
            entry_id,
            InspectBase(content_sha256=saved["state"]["record"]["hash"]),
        )

    def publish(**options):
        with Session(engine, autoflush=False) as db:
            current = base_content.read(db, tokens["owner"], share_id, entry_id)
            return publish_bytes(db, content(**options), current["state"])

    def axis():
        with Session(engine, autoflush=False) as db:
            return timeline_axes.context(
                db,
                tokens["reviewer"],
                share_id,
                entry_id,
                BaseTimelineAxisContext(view_id=view_id),
            )

    def create(kind="calendar", *, proof=None, lane=None):
        proof = proof or axis()
        value = next(case["input"] for case in RANGES if case["input"]["kind"] == kind)
        return CommandEnvelope.model_validate(
            {
                "protocol_version": 1,
                "operation_id": uuid.uuid4(),
                "share_id": share_id,
                "entry_id": entry_id,
                "command": {
                    "type": "thread.create",
                    "thread_id": uuid.uuid4(),
                    "message_id": uuid.uuid4(),
                    "body": {"version": 1, "blocks": [{"type": "code", "text": "Range review"}]},
                    "anchor": {
                        "version": 1,
                        "type": "base",
                        **{
                            key: proof[key]
                            for key in (
                                "definition_id",
                                "generation",
                                "view_id",
                                "registry_revision",
                            )
                        },
                        "target": {
                            "type": "timeline_range",
                            "axis_definition_id": proof["axis"]["id"],
                            "axis_definition_revision": proof["axis"]["revision"],
                            "axis": proof["axis"]["axis"],
                            "range": value,
                            **({"lane": lane} if lane else {}),
                        },
                    },
                },
            }
        )

    def send(payload):
        with Session(engine, autoflush=False) as db:
            return discussions.apply_command(db, tokens["reviewer"], payload)

    def read(payload):
        with Session(engine, autoflush=False) as db:
            return discussions.read_thread(
                db, tokens["reviewer"], share_id, entry_id, payload.command.thread_id
            )

    def totals():
        with Session(engine) as db:
            return tuple(
                db.scalar(select(func.count()).select_from(model))
                for model in (
                    m.DiscussionThread,
                    m.DiscussionAnchorRevision,
                    m.DiscussionMessage,
                    m.DiscussionMessageRevision,
                    m.DiscussionEvent,
                    m.DiscussionReceipt,
                )
            )

    def rejected(action, status, codes):
        try:
            action()
        except HTTPException as exc:
            assert exc.status_code == status and exc.detail["code"] in codes, exc.detail
        else:
            raise AssertionError("A stale or unauthorized range request was accepted")

    def absent(payload):
        with Session(engine) as db:
            assert db.get(m.DiscussionThread, payload.command.thread_id) is None
            assert db.get(m.DiscussionMessage, payload.command.message_id) is None
            assert db.get(m.DiscussionReceipt, (reviewer_id, payload.operation_id)) is None

    def blocked_send(payload, mutation, status, codes):
        before = totals()
        pid_queue = Queue()
        with Session(engine, autoflush=False) as editor:
            editor.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

            def worker():
                with Session(engine, autoflush=False) as db:
                    pid_queue.put(db.scalar(text("SELECT pg_backend_pid()")))
                    rejected(
                        lambda: discussions.apply_command(db, tokens["reviewer"], payload),
                        status,
                        codes,
                    )

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(worker)
                try:
                    wait_for_lock(engine, pid_queue.get(timeout=5))
                    mutation(editor)
                finally:
                    editor.rollback()
                pending.result(timeout=10)
        assert totals() == before
        absent(payload)

    accepted_ranges = []
    for kind in ("calendar", "instant", "historical", "ordinal"):
        registry = publish(sequence=kind == "ordinal")
        payload = create(kind)
        before = totals()
        barrier = threading.Barrier(2)

        def duplicate():
            barrier.wait(timeout=5)
            return send(payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = pool.submit(duplicate), pool.submit(duplicate)
            accepted = left.result(timeout=10)
            assert right.result(timeout=10) == accepted
        assert totals() == tuple(count + 1 for count in before)
        stored = read(payload)["anchor"]["target"]
        assert stored["range"] == next(
            case["normalized"] for case in RANGES if case["input"]["kind"] == kind
        )
        assert stored == payload.command.anchor.target.model_dump(mode="json")
        assert send(payload) == accepted
        accepted_ranges.append((payload, accepted))
    print("PASS: four range kinds preserve exact JSON and concurrent retries commit once")

    # Distinct operations must allocate different commit-ordered events even
    # when they describe the same interval. A fresh connection reads the log.
    parallel = [create("ordinal") for _ in range(4)]
    before = totals()
    with Session(engine) as db:
        cursor = discussions.changes(db, tokens["reviewer"], share_id, entry_id, view_id=view_id)[
            "cursor"
        ]
    barrier = threading.Barrier(len(parallel))

    def independent(payload):
        barrier.wait(timeout=5)
        return send(payload)

    with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
        receipts = list(pool.map(independent, parallel))
    sequences = sorted(receipt["sequence"] for receipt in receipts)
    assert sequences == list(range(sequences[0], sequences[0] + len(parallel)))
    assert totals() == tuple(count + len(parallel) for count in before)
    with Session(engine) as db:
        events = discussions.changes(
            db, tokens["reviewer"], share_id, entry_id, view_id=view_id, cursor=cursor
        )["events"]
        assert [event["sequence"] for event in events] == sequences
        assert {event["data"]["thread_id"] for event in events} == {
            str(payload.command.thread_id) for payload in parallel
        }
        other_view = uuid.UUID(next(v["id"] for v in registry["views"] if v["name"] == "Cards"))
        assert (
            discussions.list_threads(
                db, tokens["reviewer"], share_id, entry_id, view_id=other_view
            )["threads"]
            == []
        )
    print("PASS: independent range saves have ordered events isolated to their saved view")

    stale_payload = create("ordinal")
    old_axis = axis()["axis"]
    with Session(engine, autoflush=False, expire_on_commit=False) as stale:
        cached_axis = stale.get(m.BaseTargetDefinition, uuid.UUID(old_axis["id"]))
        stale.commit()
        publish(sequence=True, property_name="note.other")
        current = axis()["axis"]
        assert current["id"] == old_axis["id"] and current["revision"] > old_axis["revision"]
        rejected(
            lambda: discussions.apply_command(stale, tokens["reviewer"], stale_payload),
            409,
            {"timeline_axis_changed"},
        )
    absent(stale_payload)
    for payload, receipt in accepted_ranges:
        assert send(payload) == receipt
        assert read(payload)["anchor"]["target"] == payload.command.anchor.target.model_dump(
            mode="json"
        )
    print("PASS: stale ORM axis cannot authorize capture; historical ranges and retries survive")

    waiting = create("ordinal")
    with Session(engine) as db:
        expected = base_content.read(db, tokens["owner"], share_id, entry_id)["state"]
    blocked_send(
        waiting,
        lambda db: publish_bytes(
            db, content(sequence=True, property_name="note.changed"), expected
        ),
        409,
        {"base_definition_not_synced", "timeline_axis_changed"},
    )
    print("PASS: range waiting behind a real configuration publication rejects its old axis")

    # Failure after SQL flush but before commit leaves no partial anchor,
    # message, event or receipt, and does not consume the share event counter.
    retry = create("ordinal")
    before = totals()
    with Session(engine) as db:
        sequence_before = db.get(m.Share, share_id).collaboration_sequence
    with Session(engine, autoflush=False) as db:
        with patch.object(db, "commit", side_effect=RuntimeError("synthetic commit failure")):
            try:
                discussions.apply_command(db, tokens["reviewer"], retry)
            except RuntimeError as exc:
                assert str(exc) == "synthetic commit failure"
            else:
                raise AssertionError("Commit failure was swallowed")
    assert totals() == before
    absent(retry)
    receipt = send(retry)
    assert receipt["sequence"] == sequence_before + 1
    assert send(retry) == receipt
    assert totals() == tuple(count + 1 for count in before)
    print("PASS: precommit failure rolls back the entire range transaction; retry commits once")

    pending = create("ordinal")
    rule_ids = []

    def deny_comment(db):
        rule = m.ManagedCommentRule(
            share_id=share_id,
            user_id=reviewer_id,
            path=db.get(m.ManagedEntry, str(entry_id)).path,
            recursive=False,
            allowed=False,
        )
        db.add(rule)
        db.flush()
        rule_ids.append((m.ManagedCommentRule, rule.id))
        managed_service.invalidate_discussion_snapshots(db, share_id)
        db.commit()

    def restore_rules():
        with Session(engine) as db:
            db.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
            for model, identity in rule_ids:
                db.delete(db.get(model, identity))
            managed_service.invalidate_discussion_snapshots(db, share_id)
            db.commit()
        rule_ids.clear()

    blocked_send(pending, deny_comment, 403, {"comment_forbidden"})
    assert read(retry)["thread"]["id"] == receipt["thread_id"]
    restore_rules()
    send(pending)
    print("PASS: comment revocation during a lock wait rejects creation but preserves read access")

    # Inventory-backed lanes use exact source witnesses without a relay or
    # object store. A revoked source must also hide its range from reads/events.
    publish(grouped=True)
    with Session(engine, autoflush=False) as db:
        source = base_discussions.target_context(
            db,
            tokens["reviewer"],
            share_id,
            entry_id,
            BaseTargetContext(view_id=view_id, record_id=record_id),
        )
        catalog = base_target_definitions.catalog(
            db, tokens["reviewer"], share_id, entry_id, BaseTargetCatalog(view_id=view_id)
        )
        assert all(item["kind"] != "axis" for item in catalog["targets"])
        group = next(item for item in catalog["targets"] if item["kind"] == "group")
        path = db.get(m.ManagedEntry, str(record_id)).path
        db.commit()
        lane = base_groups.capture(
            db,
            tokens["reviewer"],
            share_id,
            entry_id,
            CaptureBaseGroup.model_validate(
                {
                    **{
                        key: source[key]
                        for key in (
                            "definition_id",
                            "view_id",
                            "generation",
                            "registry_revision",
                            "record_id",
                            "record_generation",
                        )
                    },
                    "operation_id": uuid.uuid4(),
                    "source_kind": "file",
                    "source_path_sha256": hashlib.sha256(path.encode()).hexdigest(),
                    "group_definition_id": group["id"],
                    "group_definition_revision": group["revision"],
                    "value": {"type": "text", "value": path.rsplit(".", 1)[-1]},
                }
            ),
        )["anchor"]["target"]
    original = create(lane=lane)
    accepted = send(original)
    pending_lane = create(lane=lane)

    def revoke_source(db):
        rule = m.ManagedRule(
            share_id=share_id,
            user_id=reviewer_id,
            path=path,
            recursive=False,
            permission="none",
        )
        db.add(rule)
        db.flush()
        rule_ids.append((m.ManagedRule, rule.id))
        managed_service.invalidate_discussion_snapshots(db, share_id)
        db.commit()

    blocked_send(pending_lane, revoke_source, 404, {"target_unavailable"})
    rejected(lambda: read(original), 404, {"target_unavailable"})
    rejected(lambda: send(original), 404, {"target_unavailable"})
    with Session(engine) as db:
        assert accepted["thread_id"] not in {
            item["id"]
            for item in discussions.list_threads(
                db, tokens["reviewer"], share_id, entry_id, view_id=view_id
            )["threads"]
        }
        assert accepted["thread_id"] not in {
            event["data"]["thread_id"]
            for event in discussions.changes(
                db, tokens["reviewer"], share_id, entry_id, view_id=view_id
            )["events"]
        }
    whole_view = create()
    send(whole_view)
    restore_rules()
    assert send(original) == accepted
    assert read(original)["anchor"]["target"]["lane"] == lane
    send(pending_lane)
    print("PASS: exact lane witness gates waiting saves, retries, reads and filtered events")

    before = totals()
    original_anchor = read(original)["anchor"]
    try:
        migrations.downgrade(Config("alembic.ini"), "202611060004")
    except RuntimeError as exc:
        assert "Export/migrate group targets" in str(exc)
    else:
        raise AssertionError("Downgrade discarded populated range/group evidence")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        )
    assert totals() == before
    assert read(original)["anchor"] == original_anchor
    assert send(original) == accepted
    print("PASS: refused downgrade preserves timeline axes, lane anchors and accepted receipts")

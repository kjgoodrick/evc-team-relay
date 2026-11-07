"""Real marker CAS, authorization lock waits and archived discussion history."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from alembic import command as migrations
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import models as m
from app.schemas.bases import BaseTimelineAxisContext
from app.schemas.discussions import CommandEnvelope
from app.schemas.timeline_markers import TimelineMarkerCatalog, TimelineMarkerCommand
from app.services import discussion_service as discussions
from app.services import managed_service, timeline_axes, timeline_markers


def prove_timeline_markers(engine, tokens, share_id, entry_id, view_id, reviewer_id, wait_for_lock):
    get_settings().collaboration_bases_timeline_markers_enabled = True

    def page():
        with Session(engine, autoflush=False) as db:
            return timeline_markers.list_markers(
                db,
                tokens["reviewer"],
                share_id,
                entry_id,
                TimelineMarkerCatalog(view_id=view_id, include_archived=True),
            )

    def operation(marker=None, archive=False):
        catalog = page()
        with Session(engine, autoflush=False) as db:
            proof = timeline_axes.context(
                db, tokens["reviewer"], share_id, entry_id, BaseTimelineAxisContext(view_id=view_id)
            )
        mutation = dict(
            type="marker.archive" if archive else "marker.put",
            marker_id=marker["id"] if marker else str(uuid.uuid4()),
            expected_marker_revision=marker["revision"] if marker else 0,
        )
        if not archive:
            mutation.update(
                content={
                    "kind": "point",
                    "label": "Release milestone",
                    "color": "#1256ab",
                    "range": {
                        "version": 1,
                        "kind": "calendar",
                        "start": "2027-02-28",
                        "end": "2027-02-28",
                        "time_zone": "America/Denver",
                        "start_inclusive": True,
                        "end_inclusive": True,
                    },
                },
                placements=[
                    {
                        "view_id": str(view_id),
                        "axis_definition_id": proof["axis"]["id"],
                        "axis_definition_revision": proof["axis"]["revision"],
                    }
                ],
            )
        return TimelineMarkerCommand.model_validate(
            dict(
                operation_id=uuid.uuid4(),
                **{
                    key: catalog[key]
                    for key in ("definition_id", "generation", "registry_revision")
                },
                expected_catalog_revision=catalog["catalog_revision"],
                command=mutation,
            )
        )

    def send(payload, actor="owner"):
        with Session(engine, autoflush=False) as db:
            return timeline_markers.apply_command(db, tokens[actor], share_id, entry_id, payload)

    def totals():
        with Session(engine) as db:
            return tuple(
                db.scalar(select(func.count()).select_from(model))
                for model in (
                    m.BaseTimelineMarkerCatalog,
                    m.BaseTimelineMarker,
                    m.BaseTimelineMarkerPlacement,
                    m.BaseTimelineMarkerRevision,
                    m.BaseTimelineMarkerReceipt,
                )
            )

    original = operation()
    barrier = threading.Barrier(2)

    def duplicate():
        barrier.wait(timeout=5)
        return send(original)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(duplicate), pool.submit(duplicate)
        accepted = first.result(timeout=10)
        assert second.result(timeout=10) == accepted
    assert totals() == (1, 1, 1, 1, 1)
    print("PASS: concurrent marker promotion stores one identity, placement, snapshot and receipt")

    requests = [operation(), operation()]
    barrier = threading.Barrier(2)

    def competing(payload):
        barrier.wait(timeout=5)
        try:
            return send(payload)
        except HTTPException as exc:
            assert (
                exc.status_code == 409 and exc.detail["code"] == "timeline_marker_catalog_changed"
            )
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing, requests))
    assert len([result for result in results if result]) == 1
    assert totals() == (1, 2, 2, 2, 2)
    print("PASS: competing promotion operations cannot both pass the same catalog revision")

    def comment():
        catalog = page()
        marker = next(item for item in catalog["markers"] if item["id"] == accepted["marker"]["id"])
        return CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=dict(
                    type="thread.create",
                    thread_id=uuid.uuid4(),
                    message_id=uuid.uuid4(),
                    body={
                        "version": 1,
                        "blocks": [{"type": "code", "text": "Milestone discussion"}],
                    },
                    anchor=dict(
                        version=1,
                        type="base",
                        view_id=view_id,
                        **{
                            key: catalog[key]
                            for key in ("definition_id", "generation", "registry_revision")
                        },
                        target=marker["target"],
                    ),
                ),
            )
        )

    thread = comment()
    with Session(engine, autoflush=False) as db:
        receipt = discussions.apply_command(db, tokens["reviewer"], thread)
    pending_comment = comment()
    archive = operation(accepted["marker"], archive=True)
    pid_queue = Queue()
    with Session(engine, autoflush=False) as editor:
        editor.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

        def waiting_comment():
            with Session(engine, autoflush=False) as db:
                pid_queue.put(db.scalar(text("SELECT pg_backend_pid()")))
                try:
                    discussions.apply_command(db, tokens["reviewer"], pending_comment)
                except HTTPException as exc:
                    assert (
                        exc.status_code == 409 and exc.detail["code"] == "timeline_marker_changed"
                    )
                else:
                    raise AssertionError("Queued creation ignored a committed marker archive")

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(waiting_comment)
            try:
                wait_for_lock(engine, pid_queue.get(timeout=5))
                archived = timeline_markers.apply_command(
                    editor, tokens["owner"], share_id, entry_id, archive
                )
            finally:
                editor.rollback()
            waiting.result(timeout=10)
    with Session(engine, autoflush=False) as db:
        assert db.get(m.DiscussionThread, pending_comment.command.thread_id) is None
        assert db.get(m.DiscussionReceipt, (reviewer_id, pending_comment.operation_id)) is None
        assert discussions.apply_command(db, tokens["reviewer"], thread) == receipt
        stored = discussions.read_thread(
            db, tokens["reviewer"], share_id, entry_id, thread.command.thread_id
        )
        assert stored["anchor"]["target"] == thread.command.anchor.target.model_dump(mode="json")
    assert send(original) == accepted
    print("PASS: archival fences a waiting comment while preserving original discussion/receipts")

    # A plain member receives temporary editor authority over this synthetic .base
    # only. The service must recheck it after waiting for the share lock.
    with Session(engine) as db:
        db.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
        rule = m.ManagedRule(
            share_id=share_id,
            user_id=reviewer_id,
            path=db.get(m.ManagedEntry, str(entry_id)).path,
            recursive=False,
            permission="write",
        )
        db.add(rule)
        db.flush()
        rule_id = rule.id
        db.commit()
    pending_edit = operation(archived["marker"])
    before = totals()
    with Session(engine, autoflush=False) as revoker:
        revoker.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())

        def waiting_edit():
            with Session(engine, autoflush=False) as db:
                pid_queue.put(db.scalar(text("SELECT pg_backend_pid()")))
                try:
                    timeline_markers.apply_command(
                        db, tokens["reviewer"], share_id, entry_id, pending_edit
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403 and exc.detail["code"] == "base_editor_required"
                else:
                    raise AssertionError("A revoked editor restored a shared marker")

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(waiting_edit)
            try:
                wait_for_lock(engine, pid_queue.get(timeout=5))
                revoker.get(m.ManagedRule, rule_id).permission = "read"
                managed_service.invalidate_discussion_snapshots(revoker, share_id)
                revoker.commit()
            finally:
                revoker.rollback()
            waiting.result(timeout=10)
    assert totals() == before
    with Session(engine) as db:
        db.execute(select(m.Share.id).where(m.Share.id == share_id).with_for_update())
        assert db.get(m.BaseTimelineMarkerReceipt, (reviewer_id, pending_edit.operation_id)) is None
        db.delete(db.get(m.ManagedRule, rule_id))
        managed_service.invalidate_discussion_snapshots(db, share_id)
        db.commit()
    print("PASS: edit-permission revocation during a lock wait prevents marker restoration")

    before = totals()
    try:
        migrations.downgrade(Config("alembic.ini"), "202611060007")
    except RuntimeError as exc:
        assert "Export/migrate shared timeline markers" in str(exc)
    else:
        raise AssertionError("A downgrade erased marker history")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611070001"
    assert totals() == before
    assert send(original) == accepted
    assert send(archive) == archived
    print("PASS: populated marker migration refuses downgrade without changing history or receipts")

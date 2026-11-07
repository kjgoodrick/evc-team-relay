"""Batched lineage checks reveal no existence details about unavailable records."""

import uuid

import pytest
from sqlalchemy import event
from test_base_discussions import base as base_fixture
from test_base_discussions import deny_record
from test_base_discussions import targets as targets_fixture
from test_base_discussions import world as world_fixture
from test_discussions import base as discussion_path
from test_discussions import headers

from app.db import models as m

base = base_fixture
targets = targets_fixture
world = world_fixture


def read(client, t, ids, *, actor="reader", expected=200, view_id=None):
    response = client.post(
        discussion_path(t.w) + "/bases/record-contexts",
        headers=headers(t.w.tokens[actor]),
        json={"view_id": view_id or t.captured["view_id"], "record_ids": ids},
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_reader_batch_reports_only_authorized_initialized_lineage(client, db_session, targets):
    t = targets
    missing = str(uuid.uuid4())
    uninitialized = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=t.w.share.id,
        path="Notes/New.md",
        record={"type": "markdown"},
        deleted=False,
    )
    db_session.add(uninitialized)
    db_session.flush()
    uninitialized.collaboration_generation = None
    db_session.commit()
    ids = [t.record.id, missing, uninitialized.id]
    result = read(client, t, ids)
    assert result == {
        **{
            key: t.captured[key]
            for key in (
                "protocol_version",
                "entry_id",
                "definition_id",
                "view_id",
                "generation",
                "registry_revision",
            )
        },
        "records": [
            {"record_id": t.record.id, "record_generation": t.captured["record_generation"]},
            {"record_id": missing, "record_generation": None},
            {"record_id": uninitialized.id, "record_generation": None},
        ],
    }
    db_session.refresh(uninitialized)
    assert uninitialized.collaboration_generation is None
    deny_record(db_session, t, actor="reader")
    denied = read(client, t, ids)
    assert all(item["record_generation"] is None for item in denied["records"])
    t.record.deleted = True
    db_session.commit()
    assert read(client, t, ids) == denied


def test_batch_observes_new_record_generation_and_requires_saved_view_access(
    client, db_session, targets
):
    t = targets
    t.record.collaboration_generation = uuid.uuid4()
    db_session.commit()
    result = read(client, t, [t.record.id])
    assert result["records"][0]["record_generation"] == str(t.record.collaboration_generation)
    assert result["records"][0]["record_generation"] != t.captured["record_generation"]
    read(client, t, [t.record.id], view_id=str(uuid.uuid4()), expected=404)
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reader.id,
            path=t.w.entry.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    read(client, t, [t.record.id], expected=404)


@pytest.mark.parametrize("count", [0, 101])
def test_record_batch_is_bounded(client, targets, count):
    read(client, targets, [str(uuid.uuid4()) for _ in range(count)], expected=422)


def test_duplicate_record_ids_are_rejected(client, targets):
    read(client, targets, [targets.record.id, targets.record.id], expected=422)


@pytest.mark.parametrize("size", [1, 50, 100])
def test_record_batch_query_count_does_not_grow_per_record(
    client, engine, db_session, targets, size
):
    t = targets
    ids = []
    for index in range(size):
        record = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=t.w.share.id,
            path=f"Notes/Batch{index}.md",
            record={"type": "markdown"},
            deleted=False,
            collaboration_generation=uuid.uuid4(),
        )
        db_session.add(record)
        ids.append(record.id)
    db_session.commit()
    statements = []

    def count(_connection, _cursor, sql, _parameters, _context, _many):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    event.listen(engine, "before_cursor_execute", count)
    try:
        result = read(client, t, ids)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(result["records"]) == size
    assert all(item["record_generation"] for item in result["records"])
    assert len(statements) <= 18
    assert not any("discussion_message" in sql for sql in statements)
    print({"records": size, "selects": len(statements)})

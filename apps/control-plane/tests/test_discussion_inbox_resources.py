"""Small-group inbox query measurements; timings are evidence, not CI gates."""

import copy
import json
import statistics
import time
import uuid

import pytest
from sqlalchemy import event
from test_discussion_inbox import listing, new_thread
from test_discussion_inbox import world as inbox_world  # noqa: F401
from test_discussions import world as discussion_world  # noqa: F401

from app.db import models as m
from app.services import discussion_inbox as inbox


@pytest.fixture
def world(request):
    return request.getfixturevalue("inbox_world")


@pytest.mark.parametrize("size,separate_notes", [(1, False), (10, False), (50, False), (50, True)])
def test_inbox_page_query_budget(client, engine, db_session, world, size, separate_notes):
    for _ in range(size):
        note = copy.copy(world)
        if separate_notes:
            note.entry = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=world.share.id,
                path=f"Notes/{uuid.uuid4()}.md",
                record={"type": "markdown"},
                deleted=False,
            )
            db_session.add(note.entry)
            db_session.commit()
        new_thread(client, note, world.reader)
    assert inbox.run_batch() == size

    samples = []
    for _ in range(3):
        statements = []

        def count(_connection, _cursor, sql, _parameters, _context, _many):
            if sql.lstrip().upper().startswith("SELECT"):
                statements.append(sql)

        event.listen(engine, "before_cursor_execute", count)
        started = time.perf_counter()
        try:
            result = listing(client, world, actor="reader", limit=50)
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count)
        assert len(result["items"]) == size and result["next_cursor"] is None
        assert all(item["subscription"]["following"] is False for item in result["items"])
        # Leave room for constant authentication work; prohibit per-item reads
        # on an ordinary Markdown page. Bodies must remain separately fetched.
        assert len(statements) <= 20
        assert not any("discussion_message_revisions" in sql for sql in statements)
        samples.append((len(statements), elapsed))

    print(
        json.dumps(
            {
                "rows": size,
                "separate_notes": separate_notes,
                "selects": [sample[0] for sample in samples],
                "median_ms": round(statistics.median(sample[1] for sample in samples) * 1000, 2),
            },
            sort_keys=True,
        )
    )

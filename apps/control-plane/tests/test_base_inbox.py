import pytest
from test_base_discussions import command, deny_record
from test_base_discussions import targets as targets_fixture
from test_bases_registry import base as base_fixture
from test_discussion_inbox import listing, mention
from test_discussion_inbox import world as inbox_world
from test_discussions import headers, send
from test_discussions import world as discussion_world  # noqa: F401

from app.services import discussion_inbox as inbox

world = inbox_world
base = base_fixture
targets = targets_fixture


@pytest.mark.parametrize("when", ["before_projection", "after_projection"])
def test_base_cell_recipient_must_retain_both_view_and_record_access(
    client, db_session, targets, when
):
    t = targets
    payload = command(t)
    payload["command"]["body"] = mention(t.w.reviewer)
    result = send(client, t.w, payload, actor="owner")
    if when == "after_projection":
        inbox.run_batch()
        assert len(listing(client, t.w, actor="reviewer")["items"]) == 1
    deny_record(db_session, t)
    inbox.run_batch()
    assert listing(client, t.w, actor="reviewer") == {"items": [], "next_cursor": None}
    path = f"/v1/collaboration/shares/{t.w.share.id}/entries/{t.w.entry.id}"
    path += f"/threads/{result['thread_id']}"
    for suffix, payload in [
        ("subscription", {"expected_version": 0, "following": True, "muted": False}),
        ("inbox", {"seen_version": 1, "action": "read"}),
    ]:
        response = client.post(
            path + "/" + suffix, headers=headers(t.w.tokens["reviewer"]), json=payload
        )
        assert response.status_code == 404

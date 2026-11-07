import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_base_discussions import base as base_fixture
from test_base_discussions import command
from test_base_discussions import targets as targets_fixture
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_search, managed_service
from app.services.discussion_search import read_predicate
from app.services.discussion_search_projection_v1 import project, words

world = discussion_world
base = base_fixture
targets = targets_fixture


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_search_enabled", True)


def search(client, w, query="", actor="reviewer", expected=200, **filters):
    response = client.post(
        f"/v1/collaboration/shares/{w.share.id}/search",
        headers=headers(w.tokens[actor]),
        json={"protocol_version": 1, "query": query, **filters},
    )
    assert response.status_code == expected, response.text
    return response.json()


def ids(page):
    return [item["thread"]["id"] for item in page["items"]]


def test_current_text_is_atomic_with_edits_deletions_and_history(client, db_session, world):
    w = world
    payload = create(w, "Initial lunar phrase")
    created = send(client, w, payload)
    result = search(client, w, "lunar")
    assert ids(result) == [created["thread_id"]]
    assert result["items"][0]["excerpt"] == "Initial lunar phrase"
    edit = envelope(
        w,
        {
            "type": "message.edit",
            "thread_id": created["thread_id"],
            "message_id": created["message_id"],
            "expected_revision": 1,
            "body": body("Changed solar phrase"),
        },
    )
    send(client, w, edit)
    assert search(client, w, "lunar")["items"] == []
    assert search(client, w, "solar")["items"][0]["message_revision"] == 2
    # A rejected stale edit cannot alter the index or immutable history.
    edit["operation_id"] = str(uuid.uuid4())
    edit["command"]["body"] = body("Rejected Venus phrase")
    send(client, w, edit, expected=409)
    assert search(client, w, "Venus")["items"] == []
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.delete",
                "thread_id": created["thread_id"],
                "message_id": created["message_id"],
                "expected_revision": 2,
            },
        ),
    )
    assert search(client, w, "solar")["items"] == []
    assert db_session.get(m.DiscussionSearchDocument, uuid.UUID(created["message_id"])) is None
    assert (
        db_session.get(m.DiscussionMessageRevision, (uuid.UUID(created["message_id"]), 1)).body
        == payload["command"]["body"]
    )


def test_mine_mentions_and_state_apply_to_current_matching_messages(client, world):
    w = world
    first = create(w, "Design review")
    first["command"]["body"]["blocks"][0]["children"].append(
        {"type": "mention", "user_id": str(w.reviewer.id)}
    )
    root = send(client, w, first, actor="owner")
    assert ids(search(client, w, "review", mentions=True)) == [root["thread_id"]]
    assert search(client, w, "review", mine=True)["items"] == []
    reply = envelope(
        w,
        {
            "type": "reply.create",
            "thread_id": root["thread_id"],
            "message_id": str(uuid.uuid4()),
            "body": body("Reviewer agrees"),
        },
    )
    saved = send(client, w, reply)
    assert search(client, w, "agrees", mine=True)["items"][0]["message_id"] == saved["message_id"]
    assert search(client, w, "agrees", mentions=True)["items"] == []
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.state",
                "thread_id": root["thread_id"],
                "state": "resolved",
                "expected_state_revision": 1,
            },
        ),
    )
    assert search(client, w, state="open")["items"] == []
    assert ids(search(client, w, state="resolved")) == [root["thread_id"]]
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": root["thread_id"],
                "message_id": root["message_id"],
                "expected_revision": 1,
                "body": body("Design review revised"),
            },
        ),
        actor="owner",
    )
    assert search(client, w, mentions=True)["items"] == []


def test_authorization_precedes_search_and_revocation_applies_to_later_pages(
    client, db_session, world
):
    w = world
    private = create(w, "Sealed phrase")
    private["entry_id"] = w.other.id
    send(client, w, private, actor="owner")
    for text in ("First visible", "Second visible"):
        send(client, w, create(w, text))
    assert search(client, w, "sealed")["items"] == []
    assert "Sealed" not in str(search(client, w))
    page = search(client, w, limit=1)
    assert len(page["items"]) == 1 and page["next_cursor"]
    db_session.add(
        m.ManagedRule(
            share_id=w.share.id,
            user_id=w.reviewer.id,
            path=w.entry.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    assert search(client, w, cursor=page["next_cursor"], limit=1)["items"] == []
    search(client, w, entry_id=w.entry.id, expected=404)
    assert len(search(client, w, "sealed", actor="owner")["items"]) == 1


def test_readers_can_search_and_cursor_is_bound_to_actor_filters_and_share_epoch(
    client, db_session, world
):
    w = world
    for n in range(3):
        send(client, w, create(w, f"Searchable {n}"))
    page = search(client, w, "searchable", actor="reader", limit=1)
    second = search(client, w, "SEARCHABLE", actor="reader", cursor=page["next_cursor"], limit=1)
    assert ids(page) != ids(second)
    assert ids(second) != ids(
        search(client, w, "searchable", actor="reader", cursor=second["next_cursor"], limit=1)
    )
    search(
        client, w, "different", actor="reader", cursor=page["next_cursor"], limit=1, expected=409
    )
    search(client, w, "searchable", cursor=page["next_cursor"], limit=1, expected=409)
    db_session.get(m.Share, w.share.id).collaboration_epoch = uuid.uuid4()
    db_session.commit()
    search(
        client, w, "searchable", actor="reader", cursor=page["next_cursor"], limit=1, expected=409
    )


def test_page_returns_one_latest_matching_message_per_thread_and_rechecks_removed_threads(
    client, world
):
    w = world
    root = send(client, w, create(w, "Repeat match"))
    replies = [
        send(
            client,
            w,
            envelope(
                w,
                {
                    "type": "reply.create",
                    "thread_id": root["thread_id"],
                    "message_id": str(uuid.uuid4()),
                    "body": body("Repeat match"),
                },
            ),
        )
        for _ in range(3)
    ]
    result = search(client, w, "repeat match")
    assert (
        len(result["items"]) == 1 and result["items"][0]["message_id"] == replies[-1]["message_id"]
    )
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.remove",
                "thread_id": root["thread_id"],
                "expected_state_revision": 1,
                "reason": "Test removal",
            },
        ),
        actor="owner",
    )
    assert search(client, w, "repeat", actor="owner")["items"] == []


def test_saved_view_and_record_intersection_authority(client, db_session, world, base, targets):
    t, w = targets, world
    cell = send(client, w, command(t))
    second = send(client, w, command(t, "view", second=True))
    first_view = t.snapshot["views"][0]["id"]
    assert ids(search(client, w, entry_id=w.entry.id, view_id=first_view)) == [cell["thread_id"]]
    search(client, w, entry_id=w.entry.id, expected=422)
    db_session.add(
        m.ManagedRule(
            share_id=w.share.id,
            user_id=w.reviewer.id,
            path=t.record.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    result = search(client, w)
    assert ids(result) == [second["thread_id"]]
    assert result["items"][0]["view_id"] == t.snapshot["views"][1]["id"]
    assert search(client, w, entry_id=w.entry.id, view_id=first_view)["items"] == []


def test_sql_path_rules_match_managed_precedence_and_literal_case(client, db_session, world):
    w = world
    entries = []
    for path in (
        "Notes/A.md",
        "notes/A.md",
        "Notes/Exact.md",
        "Notes/Sub/A.md",
        "Notes2/A.md",
        "Odd_%/A.md",
        "Odd_x/A.md",
    ):
        entry = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=w.share.id,
            path=path,
            record={"type": "markdown"},
            deleted=False,
        )
        entries.append(entry)
        db_session.add(entry)
    for path, recursive, permission in [
        ("", True, "none"),
        ("Notes/Sub", True, "none"),
        ("Notes/Exact.md", False, "none"),
        ("Odd_%", True, "read"),
    ]:
        db_session.add(
            m.ManagedRule(
                share_id=w.share.id,
                user_id=w.reviewer.id,
                path=path,
                recursive=recursive,
                permission=permission,
            )
        )
    db_session.commit()
    rules = managed_service.rules_for(db_session, w.share, w.reviewer)
    expected = {
        entry.id
        for entry in entries
        if managed_service.permission(w.share, w.reviewer, entry.path, rules) in {"read", "write"}
    }
    actual = set(
        db_session.scalars(
            select(m.ManagedEntry.id).where(
                m.ManagedEntry.id.in_([e.id for e in entries]),
                read_predicate(w.share, w.reviewer, rules, m.ManagedEntry.path),
            )
        )
    )
    assert actual == expected


def test_projection_keeps_visible_text_and_current_mention_ids_without_urls_or_markup():
    user = uuid.uuid4()
    payload = {
        "version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "children": [
                    {"type": "text", "text": "Con", "marks": ["bold"]},
                    {"type": "text", "text": "tiguous "},
                    {
                        "type": "link",
                        "url": "https://hidden.example/secret",
                        "children": [{"type": "text", "text": "Visible label"}],
                    },
                    {"type": "mention", "user_id": str(user)},
                ],
            },
            {
                "type": "list",
                "items": [
                    {"type": "paragraph", "children": [{"type": "text", "text": "Straße Café"}]}
                ],
            },
            {"type": "code", "text": "alpha_beta()", "language": "secretlang"},
            {"type": "picture", "picture_id": str(uuid.uuid4()), "alt": "A diagram"},
        ],
    }
    text, terms, mentions = project(payload)
    assert text == "Contiguous Visible label \nStraße Café\nalpha_beta()\nA diagram"
    assert "strasse" in terms and "café" in terms and "alpha beta" in terms
    assert "secret" not in terms and str(user) not in terms and mentions == {user}
    assert words("STRASSE Ｃａｆé") == ["strasse", "café"]


@pytest.mark.parametrize(
    "filters",
    [
        {"query": "\x00Secret"},
        {"query": "x" * 257},
        {"query": " ".join(f"x{i}" for i in range(33))},
        {"query": "!!!"},
        {"limit": True},
        {"mine": "true"},
        {"view_id": str(uuid.uuid4())},
        {"unknown": "Secret"},
    ],
)
def test_invalid_queries_are_bounded_and_not_reflected_in_errors(client, world, filters):
    response = client.post(
        f"/v1/collaboration/shares/{world.share.id}/search",
        headers=headers(world.tokens["reviewer"]),
        json=filters,
    )
    assert response.status_code == 422
    assert "Secret" not in response.text


def test_disabled_search_does_not_disable_index_maintenance(client, db_session, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_search_enabled", False)
    result = send(client, world, create(world, "Still indexed"))
    search(client, world, expected=404)
    assert (
        db_session.get(m.DiscussionSearchDocument, uuid.UUID(result["message_id"])).plain_text
        == "Still indexed"
    )


def test_bounded_scan_continues_across_nonmatches_without_losing_results(
    client, world, monkeypatch
):
    monkeypatch.setattr(discussion_search, "SCAN_LIMIT", 2)
    wanted = send(client, world, create(world, "Wanted oldest comment"))
    for n in range(4):
        send(client, world, create(world, f"Other discussion {n}"))
    first = search(client, world, "wanted")
    assert first["items"] == [] and first["next_cursor"]
    second = search(client, world, "wanted", cursor=first["next_cursor"])
    assert second["items"] == [] and second["next_cursor"]
    third = search(client, world, "wanted", cursor=second["next_cursor"])
    assert ids(third) == [wanted["thread_id"]] and third["next_cursor"] is None
    assert "count" not in third


def test_database_failure_never_reflects_private_query_parameters(client, world, monkeypatch):
    def fail(*args):
        raise OperationalError("SELECT :query", {"query": "Secret phrase"}, RuntimeError("timeout"))

    monkeypatch.setattr(discussion_search, "_search", fail)
    response = client.post(
        f"/v1/collaboration/shares/{world.share.id}/search",
        headers=headers(world.tokens["reviewer"]),
        json={"query": "Secret phrase"},
    )
    assert (
        response.status_code == 503
        and "Secret" not in response.text
        and "SELECT" not in response.text
    )

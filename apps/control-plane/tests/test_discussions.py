import base64
import copy
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.schemas.discussions import CommandEnvelope
from app.services import anchor_service, discussion_service, session_service


def body(text="First comment"):
    return {
        "version": 1,
        "blocks": [
            {"type": "paragraph", "children": [{"type": "text", "text": text, "marks": []}]}
        ],
    }


def headers(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def world(db_session, test_user, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "collaboration_policy_enabled", True)
    monkeypatch.setattr(settings, "collaboration_discussions_enabled", True)
    db_session.autoflush = False  # Match production, including direct service calls.
    reviewer = m.User(email="reviewer@discussion.test", password_hash="", is_active=True)
    reader = m.User(email="reader@discussion.test", password_hash="", is_active=True)
    db_session.add_all([reviewer, reader])
    db_session.flush()
    share = m.Share(
        kind=m.ShareKind.FOLDER,
        path="Team",
        managed=True,
        visibility=m.ShareVisibility.PRIVATE,
        owner_user_id=test_user.id,
    )
    db_session.add(share)
    db_session.flush()
    entry = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=share.id,
        path="Notes/One.md",
        record={"type": "markdown"},
        deleted=False,
    )
    other = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=share.id,
        path="Private/Two.md",
        record={"type": "markdown"},
        deleted=False,
    )
    db_session.add_all([entry, other])
    for user in (reviewer, reader):
        db_session.add(
            m.ShareMember(share_id=share.id, user_id=user.id, role=m.ShareMemberRole.VIEWER)
        )
        db_session.add(
            m.ManagedRule(
                share_id=share.id, user_id=user.id, path="Notes", recursive=True, permission="read"
            )
        )
    db_session.add(
        m.ManagedCommentRule(
            share_id=share.id, user_id=reviewer.id, path="Notes", recursive=True, allowed=True
        )
    )
    tokens, sessions = {}, {}
    for name, user in (("owner", test_user), ("reviewer", reviewer), ("reader", reader)):
        session, _ = session_service.create_session(db_session, user.id)
        sessions[name] = session.id
        tokens[name] = security.create_access_token(str(user.id), session_id=str(session.id))
    db_session.commit()
    return SimpleNamespace(
        share=share,
        entry=entry,
        other=other,
        owner=test_user,
        reviewer=reviewer,
        reader=reader,
        tokens=tokens,
        sessions=sessions,
    )


def envelope(w, command, **extra):
    return {
        "protocol_version": 1,
        "operation_id": str(uuid.uuid4()),
        "share_id": str(w.share.id),
        "entry_id": w.entry.id,
        "command": command,
        **extra,
    }


def create(w, text="First comment"):
    return envelope(
        w,
        {
            "type": "thread.create",
            "thread_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "anchor": {"version": 1, "type": "note"},
            "body": body(text),
        },
    )


def send(client, w, payload, actor="reviewer", expected=200):
    response = client.post(
        "/v1/collaboration/commands", headers=headers(w.tokens[actor]), json=payload
    )
    assert response.status_code == expected, response.text
    return response.json()


def base(w, entry=None):
    return f"/v1/collaboration/shares/{w.share.id}/entries/{entry or w.entry.id}"


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_participant_directory_is_entry_authorized_and_pages_without_hidden_members(
    client, db_session, world
):
    w = world
    private_user = m.User(email="private@discussion.test", password_hash="", is_active=True)
    db_session.add(private_user)
    db_session.flush()
    db_session.add(
        m.ShareMember(share_id=w.share.id, user_id=private_user.id, role=m.ShareMemberRole.VIEWER)
    )
    db_session.add(
        m.ManagedRule(
            share_id=w.share.id,
            user_id=private_user.id,
            path="Private",
            recursive=True,
            permission="read",
        )
    )
    db_session.commit()
    seen, after = [], None
    while True:
        response = client.get(
            base(w) + "/participants",
            params={"limit": 1, **({"after": after} if after else {})},
            headers=headers(w.tokens["reviewer"]),
        )
        assert response.status_code == 200
        page = response.json()
        seen.extend(page["participants"])
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert {person["id"] for person in seen} == {
        str(w.owner.id),
        str(w.reviewer.id),
        str(w.reader.id),
    }
    assert all(set(person) == {"id", "name"} and "@" not in person["name"] for person in seen)
    assert (
        client.get(
            base(w, w.other.id) + "/participants", headers=headers(w.tokens["reviewer"])
        ).status_code
        == 404
    )


def test_targeted_message_read_tracks_edits_reactions_and_tombstones(client, world):
    w = world
    payload = create(w)
    result = send(client, w, payload)
    tid, mid = result["thread_id"], result["message_id"]
    path = base(w) + f"/threads/{tid}/messages/{mid}"
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": tid,
                "message_id": mid,
                "expected_revision": 1,
                "body": body("Edited"),
            },
        ),
    )
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "reaction.set",
                "thread_id": tid,
                "message_id": mid,
                "emoji": "heart",
                "present": True,
            },
        ),
    )
    response = client.get(path, headers=headers(w.tokens["reviewer"]))
    assert response.status_code == 200
    data = response.json()
    assert data["thread"]["activity_sequence"] == 3
    assert data["anchor"] == {"type": "note", "version": 1, "entry_id": w.entry.id}
    assert data["message"]["revision"] == 2
    assert data["message"]["body"] == body("Edited")
    assert data["message"]["reactions"] == [{"emoji": "heart", "count": 1, "mine": True}]
    reader = client.get(path, headers=headers(w.tokens["reader"]))
    assert reader.status_code == 200
    assert reader.json()["message"]["reactions"] == [{"emoji": "heart", "count": 1, "mine": False}]
    send(
        client,
        w,
        envelope(
            w,
            {"type": "message.delete", "thread_id": tid, "message_id": mid, "expected_revision": 2},
        ),
    )
    tombstone = client.get(path, headers=headers(w.tokens["reader"])).json()["message"]
    assert tombstone["deleted"] and tombstone["body"] is None and tombstone["reactions"] == []


def test_targeted_message_read_obeys_entry_thread_and_current_session_access(
    client, db_session, world
):
    w = world
    one, two = send(client, w, create(w)), send(client, w, create(w))
    path = base(w) + f"/threads/{one['thread_id']}/messages/{one['message_id']}"
    mixed = base(w) + f"/threads/{two['thread_id']}/messages/{one['message_id']}"
    assert client.get(mixed, headers=headers(w.tokens["reader"])).status_code == 404
    private = base(w, w.other.id) + f"/threads/{one['thread_id']}/messages/{one['message_id']}"
    assert client.get(private, headers=headers(w.tokens["reader"])).status_code == 404
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.remove",
                "thread_id": one["thread_id"],
                "expected_state_revision": 1,
                "reason": "Removed",
            },
        ),
        actor="owner",
    )
    assert client.get(path, headers=headers(w.tokens["reader"])).status_code == 404
    state = client.get(
        base(w) + f"/threads/{one['thread_id']}/state", headers=headers(w.tokens["reader"])
    )
    assert state.status_code == 200
    assert state.json()["thread"]["removed"] is True
    assert set(state.json()) == {"thread"}
    assert client.get(path, headers=headers(w.tokens["owner"])).status_code == 200
    session = db_session.get(m.UserSession, w.sessions["owner"])
    db_session.delete(session)
    db_session.commit()
    assert client.get(path, headers=headers(w.tokens["owner"])).status_code == 401


def test_commenter_can_discuss_without_note_write_and_retries_are_exactly_once(
    client, db_session, world
):
    w = world
    payload = create(w)
    accepted = send(client, w, payload)
    assert accepted == send(client, w, payload)
    assert accepted["sequence"] == 1 and accepted["message_revision"] == 1
    assert (
        count(db_session, m.DiscussionThread)
        == count(db_session, m.DiscussionMessage)
        == count(db_session, m.DiscussionReceipt)
        == count(db_session, m.DiscussionEvent)
        == 1
    )
    assert db_session.get(m.ManagedEntry, w.entry.id).record == {"type": "markdown"}
    assert (
        client.post(
            "/v1/tokens/relay",
            headers=headers(w.tokens["reviewer"]),
            json={"share_id": str(w.share.id), "doc_id": w.entry.id, "mode": "write"},
        ).status_code
        == 403
    )
    conflicting = copy.deepcopy(payload)
    conflicting["command"]["body"] = body("different content")
    assert (
        send(client, w, conflicting, expected=409)["error"]["message"]["code"]
        == "operation_id_conflict"
    )
    send(client, w, create(w), actor="reader", expected=403)
    assert count(db_session, m.DiscussionEvent) == 1


def test_commands_work_with_production_session_configuration(db_session, world):
    w = world
    payload = CommandEnvelope.model_validate(create(w))
    result = discussion_service.apply_command(db_session, w.tokens["reviewer"], payload)
    db_session.expire_all()
    assert db_session.get(m.Share, w.share.id).collaboration_sequence == result["sequence"] == 1
    assert discussion_service.apply_command(db_session, w.tokens["reviewer"], payload) == result
    assert count(db_session, m.DiscussionMessageRevision) == 1


def test_edits_are_author_only_revision_checked_and_preserve_history(client, db_session, world):
    w = world
    made = send(client, w, create(w))
    edit = envelope(
        w,
        {
            "type": "message.edit",
            "thread_id": made["thread_id"],
            "message_id": made["message_id"],
            "expected_revision": 1,
            "body": body("Edited"),
        },
    )
    send(client, w, edit, actor="owner", expected=403)
    edited = send(client, w, edit)
    assert edited["message_revision"] == 2
    stale = copy.deepcopy(edit)
    stale["operation_id"] = str(uuid.uuid4())
    send(client, w, stale, expected=409)
    url = base(w) + f"/threads/{made['thread_id']}/messages/{made['message_id']}/history"
    revisions = client.get(url, headers=headers(w.tokens["reader"])).json()["revisions"]
    assert [r["body"] for r in revisions] == [body(), body("Edited")]
    db_session.expire_all()
    assert db_session.get(m.Share, w.share.id).collaboration_sequence == 2
    assert count(db_session, m.DiscussionReceipt) == 2


def test_resolution_reply_and_atomic_send_reopen(client, db_session, world):
    w = world
    made = send(client, w, create(w))
    state = envelope(
        w,
        {
            "type": "thread.state",
            "thread_id": made["thread_id"],
            "expected_state_revision": 1,
            "state": "resolved",
        },
    )
    resolved = send(client, w, state)
    reply = envelope(
        w,
        {
            "type": "reply.create",
            "thread_id": made["thread_id"],
            "message_id": str(uuid.uuid4()),
            "body": body("Still resolved"),
        },
    )
    assert send(client, w, reply)["state"] == "resolved"
    reopen = envelope(
        w,
        {
            "type": "reply.create",
            "thread_id": made["thread_id"],
            "message_id": str(uuid.uuid4()),
            "body": body("Reopen"),
            "reopen": True,
            "expected_state_revision": 1,
        },
    )
    send(client, w, reopen, expected=409)
    assert count(db_session, m.DiscussionMessage) == 2
    assert count(db_session, m.DiscussionEvent) == 3
    reopen["command"]["expected_state_revision"] = resolved["state_revision"]
    reopened = send(client, w, reopen)
    assert (reopened["state"], reopened["state_revision"]) == ("open", 3)
    assert send(client, w, reopen) == reopened
    assert count(db_session, m.DiscussionMessage) == 3


def test_reactions_are_set_operations_and_do_not_duplicate(client, db_session, world):
    w = world
    made = send(client, w, create(w))
    reaction = envelope(
        w,
        {
            "type": "reaction.set",
            "thread_id": made["thread_id"],
            "message_id": made["message_id"],
            "emoji": "heart",
            "present": True,
        },
    )
    send(client, w, reaction)
    send(client, w, reaction)
    reaction["operation_id"] = str(uuid.uuid4())
    assert send(client, w, reaction)["changed"] is False
    assert count(db_session, m.DiscussionReaction) == 1
    send(client, w, reaction, actor="owner")
    assert count(db_session, m.DiscussionReaction) == 2
    reaction["operation_id"] = str(uuid.uuid4())
    reaction["command"]["present"] = False
    send(client, w, reaction)
    assert count(db_session, m.DiscussionReaction) == 1


def test_message_removal_is_a_tombstone_and_history_does_not_bypass_it(client, world):
    w = world
    made = send(client, w, create(w))
    remove = envelope(
        w,
        {
            "type": "message.delete",
            "thread_id": made["thread_id"],
            "message_id": made["message_id"],
            "expected_revision": 1,
        },
    )
    send(client, w, remove, actor="owner", expected=403)
    remove["command"]["reason"] = "Moderator removal"
    send(client, w, remove, actor="owner")
    url = base(w) + "/threads/" + made["thread_id"]
    message = client.get(url, headers=headers(w.tokens["reader"])).json()["messages"][0]
    assert message["body"] is None and message["deleted"]
    history = url + f"/messages/{made['message_id']}/history"
    assert client.get(history, headers=headers(w.tokens["reader"])).status_code == 404
    own_history = client.get(history, headers=headers(w.tokens["reviewer"])).json()["revisions"]
    assert own_history[-1]["reason"] == "Moderator removal"
    assert own_history[0]["body"] == body()


def test_thread_removal_hides_content_but_delivers_tombstone(client, world):
    w = world
    made = send(client, w, create(w))
    remove = envelope(
        w,
        {
            "type": "thread.remove",
            "thread_id": made["thread_id"],
            "expected_state_revision": 1,
            "reason": "Moderation",
        },
    )
    send(client, w, remove, expected=403)
    send(client, w, remove, actor="owner")
    assert (
        client.get(base(w) + "/threads", headers=headers(w.tokens["reader"])).json()["threads"]
        == []
    )
    assert (
        client.get(
            base(w) + "/threads/" + made["thread_id"], headers=headers(w.tokens["reviewer"])
        ).status_code
        == 404
    )
    changes = client.get(base(w) + "/changes", headers=headers(w.tokens["reader"])).json()
    assert changes["events"][-1]["data"]["removed"]
    assert "First comment" not in str(changes)


def test_foreign_scope_ids_and_duplicate_entity_ids_cannot_create_orphans(
    client, db_session, world
):
    w = world
    payload = create(w)
    made = send(client, w, payload)
    cross = envelope(
        w,
        {
            "type": "reply.create",
            "thread_id": made["thread_id"],
            "message_id": str(uuid.uuid4()),
            "body": body(),
        },
        entry_id=w.other.id,
    )
    send(client, w, cross, expected=404)
    send(client, w, cross, actor="owner", expected=404)
    collision = create(w)
    collision["command"]["message_id"] = made["message_id"]
    send(client, w, collision, expected=409)
    assert count(db_session, m.DiscussionThread) == count(db_session, m.DiscussionMessage) == 1
    assert (
        client.get(
            base(w, w.other.id) + "/threads/" + made["thread_id"],
            headers=headers(w.tokens["owner"]),
        ).status_code
        == 404
    )
    payload["entry_id"] = w.other.id
    send(
        client, w, payload, actor="owner", expected=409
    )  # entity ID collision, no reuse across scopes


@pytest.mark.parametrize(
    "revocation", ["comment", "read", "membership", "session", "account", "entry"]
)
def test_replay_and_reads_recheck_revocation(client, db_session, world, revocation):
    w = world
    payload = create(w)
    send(client, w, payload)
    if revocation == "comment":
        rule = db_session.scalar(
            select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == w.reviewer.id)
        )
        rule.allowed = False
    elif revocation == "read":
        rule = db_session.scalar(
            select(m.ManagedRule).where(m.ManagedRule.user_id == w.reviewer.id)
        )
        rule.permission = "none"
    elif revocation == "membership":
        db_session.delete(
            db_session.scalar(select(m.ShareMember).where(m.ShareMember.user_id == w.reviewer.id))
        )
    elif revocation == "session":
        session_service.revoke_session(db_session, w.sessions["reviewer"])
    elif revocation == "account":
        w.reviewer.is_active = False
    else:
        w.entry.deleted = True
    db_session.commit()
    status = (
        403 if revocation == "comment" else 401 if revocation in {"session", "account"} else 404
    )
    send(client, w, payload, expected=status)
    read = client.get(base(w) + "/changes", headers=headers(w.tokens["reviewer"]))
    assert read.status_code == (200 if revocation == "comment" else status)
    assert count(db_session, m.DiscussionEvent) == 1


def test_mentions_are_structured_and_require_readable_membership(client, world):
    w = world
    payload = create(w)
    mention = {"type": "mention", "user_id": str(w.reader.id)}
    payload["command"]["body"]["blocks"][0]["children"].append(mention)
    made = send(client, w, payload)
    saved = client.get(
        base(w) + "/threads/" + made["thread_id"], headers=headers(w.tokens["reader"])
    ).json()
    assert saved["messages"][0]["body"]["blocks"][0]["children"][-1] == mention
    payload = create(w)
    payload["command"]["body"]["blocks"][0]["children"] = [
        {"type": "mention", "user_id": str(uuid.uuid4())}
    ]
    send(client, w, payload, expected=422)


def test_pagination_filtered_events_and_opaque_bound_cursors(client, db_session, world):
    w = world
    first = send(client, w, create(w, "One"))
    hidden = create(w, "Private text")
    hidden["entry_id"] = w.other.id
    send(client, w, hidden, actor="owner")
    send(client, w, create(w, "Three"))
    url = base(w) + "/changes"
    page = client.get(url + "?limit=1", headers=headers(w.tokens["reviewer"])).json()
    assert len(page["events"]) == 1 and page["has_more"]
    assert page["events"][0]["sequence"] == first["sequence"]
    following = client.get(
        url, params={"cursor": page["cursor"]}, headers=headers(w.tokens["reviewer"])
    ).json()
    assert [e["sequence"] for e in following["events"]] == [3]
    assert "Private text" not in str(following)
    assert not following["has_more"]
    assert (
        client.get(
            url, params={"cursor": page["cursor"]}, headers=headers(w.tokens["owner"])
        ).status_code
        == 409
    )
    assert (
        client.get(
            url,
            params={"cursor": page["cursor"][:-4] + "oops"},
            headers=headers(w.tokens["reviewer"]),
        ).status_code
        == 409
    )
    threads = client.get(base(w) + "/threads?limit=1", headers=headers(w.tokens["reader"])).json()
    assert threads["has_more"]
    next_page = client.get(
        base(w) + "/threads",
        params={"after": threads["next_after"]},
        headers=headers(w.tokens["reader"]),
    ).json()
    assert len(next_page["threads"]) == 1 and not next_page["has_more"]
    db_session.expire_all()
    w.share.collaboration_epoch = uuid.uuid4()
    db_session.commit()
    assert (
        client.get(
            url, params={"cursor": following["cursor"]}, headers=headers(w.tokens["reviewer"])
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "text_anchor",
        "html",
        "javascript",
        "credentials",
        "blank",
        "author",
        "reopen_without_revision",
    ],
)
def test_invalid_or_unsupported_payloads_fail_before_any_write(client, db_session, world, mutation):
    w = world
    payload = create(w)
    command = payload["command"]
    if mutation == "text_anchor":
        command["anchor"]["type"] = "text"
    elif mutation == "html":
        command["body"]["blocks"] = [{"type": "html", "value": "<script>alert(1)</script>"}]
    elif mutation in {"javascript", "credentials"}:
        command["body"]["blocks"][0]["children"] = [
            {
                "type": "link",
                "url": "javascript:alert(1)"
                if mutation == "javascript"
                else "https://secret:pass@example.com",
                "children": [{"type": "text", "text": "Link"}],
            }
        ]
    elif mutation == "blank":
        command["body"] = body("  \n")
    elif mutation == "author":
        command["author_id"] = str(w.owner.id)
    else:
        payload["command"] = {
            "type": "reply.create",
            "thread_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "body": body(),
            "reopen": True,
        }
    send(client, w, payload, expected=422)
    assert count(db_session, m.DiscussionEvent) == count(db_session, m.DiscussionThread) == 0


def test_preview_gates_and_actual_byte_limits(client, db_session, world, monkeypatch):
    w = world
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", False)
    send(client, w, create(w), expected=404)
    monkeypatch.setattr(get_settings(), "collaboration_discussions_enabled", True)
    oversized = client.post(
        "/v1/collaboration/commands",
        headers={**headers(w.tokens["reviewer"]), "Content-Type": "application/json"},
        content='{"data":"' + "x" * 65536 + '"}',
    )
    assert oversized.status_code == 413
    assert (
        client.get(
            base(w) + "/threads?limit=101", headers=headers(w.tokens["reviewer"])
        ).status_code
        == 422
    )
    assert count(db_session, m.DiscussionEvent) == 0


def test_large_message_pages_are_byte_bounded_without_losing_pagination(client, world):
    w = world
    large_body = body("z" * 8000)
    large_body["blocks"] *= 3
    payload = create(w)
    payload["command"]["body"] = large_body
    first = send(client, w, payload)
    for _ in range(19):
        send(
            client,
            w,
            envelope(
                w,
                {
                    "type": "reply.create",
                    "thread_id": first["thread_id"],
                    "message_id": str(uuid.uuid4()),
                    "body": large_body,
                },
            ),
        )
    url = base(w) + "/threads/" + first["thread_id"]
    response = client.get(url, params={"limit": 100}, headers=headers(w.tokens["reader"]))
    assert response.status_code == 200 and len(response.content) < 512 * 1024
    page = response.json()
    assert page["has_more"] and 0 < len(page["messages"]) < 20
    second = client.get(
        url, params={"after": page["next_after"]}, headers=headers(w.tokens["reader"])
    ).json()
    assert len(page["messages"]) + len(second["messages"]) == 20
    assert not second["has_more"]


@pytest.mark.parametrize("field", ["protocol_version", "body", "anchor"])
def test_boolean_protocol_version_is_rejected(client, world, field):
    payload = create(world)
    if field == "protocol_version":
        payload[field] = True
    else:
        payload["command"][field]["version"] = True
    send(client, world, payload, expected=422)


def test_reaction_summary_counts_and_current_user_are_bounded(client, world):
    w = world
    made = send(client, w, create(w))
    command = {
        "type": "reaction.set",
        "thread_id": made["thread_id"],
        "message_id": made["message_id"],
        "emoji": "eyes",
        "present": True,
    }
    send(client, w, envelope(w, command))
    send(client, w, envelope(w, command), actor="owner")
    url = base(w) + "/threads/" + made["thread_id"]
    own = client.get(url, headers=headers(w.tokens["reviewer"])).json()
    reader = client.get(url, headers=headers(w.tokens["reader"])).json()
    assert own["messages"][0]["reactions"] == [{"emoji": "eyes", "count": 2, "mine": True}]
    assert reader["messages"][0]["reactions"] == [{"emoji": "eyes", "count": 2, "mine": False}]


def test_invalid_unicode_is_rejected_without_echoing_the_body(client, db_session, world):
    import json

    payload = json.dumps(create(world)).replace("First comment", r"\ud800")
    response = client.post(
        "/v1/collaboration/commands",
        headers={**headers(world.tokens["reviewer"]), "Content-Type": "application/json"},
        content=payload,
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == {"code": "invalid_command"}
    assert count(db_session, m.DiscussionThread) == 0


def test_discussions_require_revocable_sessions_and_handle_expiry_during_auth(
    client, world, monkeypatch
):
    from jwt.exceptions import ExpiredSignatureError

    w = world
    legacy = security.create_access_token(str(w.reviewer.id))
    assert (
        client.post(
            "/v1/collaboration/commands", headers=headers(legacy), json=create(w)
        ).status_code
        == 401
    )
    real_decode = security.decode_access_token
    calls = 0

    def expiring(token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExpiredSignatureError("Expired during auth")
        return real_decode(token)

    monkeypatch.setattr(security, "decode_access_token", expiring)
    send(client, w, create(w), expected=401)


def test_entry_point_does_not_discard_unrelated_pending_writes(db_session, world):
    payload = CommandEnvelope.model_validate(create(world))
    world.reviewer.email = "pending@fixture.test"
    with pytest.raises(RuntimeError, match="without pending writes"):
        discussion_service.apply_command(db_session, world.tokens["reviewer"], payload)
    assert world.reviewer.email == "pending@fixture.test"
    assert world.reviewer in db_session.dirty


def text_command(w):
    payload = create(w)
    payload["command"]["anchor"] = {
        "version": 1,
        "type": "text",
        "document_id": w.entry.id,
        "generation": str(w.entry.collaboration_generation),
        "dependency": "AAEqEA==",
        "start": "ACoGAA==",
        "end": "ACoJQQ==",
        "selected_length": 4,
        "context": {"quote": "beta", "prefix": "alpha ", "suffix": " gamma", "truncated": False},
    }
    return payload


def enable_anchors(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_text_anchors_enabled", True)
    monkeypatch.setattr(
        get_settings(), "collaboration_anchor_relay_token", SecretStr("private-fixture-token")
    )


def test_block_structure_does_not_change_legacy_anchor_serialization(world):
    envelope = CommandEnvelope.model_validate(text_command(world))
    anchor = envelope.model_dump(mode="json")["command"]["anchor"]
    assert "structure" not in anchor
    original = json.loads(envelope.canonical_json())
    original["command"]["anchor"].pop("structure", None)
    assert envelope.canonical_json() == json.dumps(
        original, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_read_only_commenter_can_create_bounded_block_anchor(client, world, monkeypatch):
    enable_anchors(monkeypatch)
    requests = relay_mock(monkeypatch)
    payload = text_command(world)
    structure = {
        "kind": "blocks",
        "blocks": [{"kind": "paragraph", "explicit_id": "existing"}, {"kind": "table"}],
    }
    payload["command"]["anchor"]["structure"] = structure
    result = send(client, world, payload)
    assert send(client, world, payload) == result and len(requests) == 2
    view = client.get(
        base(world) + "/threads/" + result["thread_id"], headers=headers(world.tokens["reviewer"])
    )
    assert view.json()["anchor"]["structure"] == structure
    assert "structure" not in json.dumps(requests)
    bad = text_command(world)
    bad["command"]["anchor"]["structure"] = {"kind": "blocks", "blocks": [{"kind": "table"}] * 33}
    send(client, world, bad, expected=422)
    bad["command"]["anchor"]["structure"]["blocks"] = [{"kind": "pixel"}]
    send(client, world, bad, expected=422)
    assert len(requests) == 2


def relay_mock(monkeypatch, change=None):
    original_client = httpx.Client
    requests = []

    def handle(request):
        assert str(request.url).startswith("http://relay-server:8080/internal/collaboration/")
        assert request.headers["authorization"] == "Bearer private-fixture-token"
        value = json.loads(request.content)
        requests.append(value)
        result = {
            key: value[key]
            for key in (
                "protocol_version",
                "request_id",
                "document_id",
                "share_id",
                "generation",
                "expires_at_ms",
            )
        }
        result.update(
            request_sha256=hashlib.sha256(request.content).hexdigest(),
            checkpointed_at_ms=anchor_service.now_ms(),
        )
        if "anchors" in value:
            result.update(
                placements=[{"status": "attached", "from": 6, "to": 10}],
                observed_snapshot_sha256="a" * 64,
            )
        status = 200
        if change:
            status, result = change(value, result)
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(result).encode()))

    def factory(**options):
        assert options == {"follow_redirects": False, "trust_env": False}
        return original_client(transport=httpx.MockTransport(handle), **options)

    monkeypatch.setattr(anchor_service.httpx, "Client", factory)
    return requests


def proof(envelope, **changes):
    return anchor_service.AnchorProof(
        **{
            "command_sha256": anchor_service.command_hash(envelope),
            "entry_id": envelope.entry_id,
            "share_id": envelope.share_id,
            "generation": envelope.command.anchor.generation,
            "expires_at_ms": anchor_service.now_ms() + 15000,
            "deadline": time.monotonic() + 12,
            "observed_snapshot_sha256": "a" * 64,
            "placement": "attached",
            **changes,
        }
    )


def test_text_anchor_context_is_server_owned_stable_and_read_authorized(
    client, db_session, world, monkeypatch
):
    w = world
    enable_anchors(monkeypatch)
    w.entry.collaboration_generation = None
    db_session.commit()
    url = base(w) + "/anchor-context"
    first = client.get(url, headers=headers(w.tokens["reviewer"]))
    assert first.status_code == 200
    generation = first.json()["generation"]
    assert client.get(url, headers=headers(w.tokens["reader"])).json()["generation"] == generation
    assert (
        client.get(
            base(w, w.other.id) + "/anchor-context", headers=headers(w.tokens["reviewer"])
        ).status_code
        == 404
    )
    db_session.expire_all()
    assert str(w.entry.collaboration_generation) == generation
    monkeypatch.setattr(get_settings(), "collaboration_text_anchors_enabled", False)
    assert client.get(url, headers=headers(w.tokens["reviewer"])).status_code == 404


def test_comment_only_text_anchor_uses_private_proof_and_duplicate_needs_no_relay(
    client, db_session, world, monkeypatch
):
    enable_anchors(monkeypatch)
    requests = relay_mock(monkeypatch)
    payload = text_command(world)
    result = send(client, world, payload)
    assert len(requests) == 2 and "anchors" not in requests[0]
    assert set(requests[1]["anchors"][0]) == {"dependency", "start", "end", "selected_length"}
    assert "First comment" not in json.dumps(requests) and "beta" not in json.dumps(requests)
    assert send(client, world, payload) == result and len(requests) == 2
    view = client.get(
        base(world) + "/threads/" + result["thread_id"], headers=headers(world.tokens["reviewer"])
    )
    assert view.status_code == 200
    assert view.json()["anchor"] == {**payload["command"]["anchor"], "entry_id": world.entry.id}
    assert "private-fixture-token" not in view.text
    bad = copy.deepcopy(payload)
    bad["operation_id"] = str(uuid.uuid4())
    bad["command"]["anchor"]["receipt"] = {"status": "attached"}
    send(client, world, bad, expected=422)
    assert len(requests) == 2
    # A reader without the separate comment grant cannot trigger the private hop.
    send(client, world, text_command(world), actor="reader", expected=403)
    assert len(requests) == 2


@pytest.mark.parametrize("change", ["permission", "session", "generation", "entry"])
def test_text_anchor_rechecks_authority_after_releasing_sql_for_network(
    db_session, world, monkeypatch, change
):
    enable_anchors(monkeypatch)
    payload = CommandEnvelope.model_validate(text_command(world))
    token, user_id, session_id = (
        world.tokens["reviewer"],
        world.reviewer.id,
        world.sessions["reviewer"],
    )
    share_id, entry_id = world.share.id, world.entry.id

    def validate(envelope):
        assert not db_session.in_transaction(), "Network work must not hold an ORM transaction"
        result = proof(envelope)
        if change == "permission":
            rule = db_session.scalar(
                select(m.ManagedCommentRule).where(
                    m.ManagedCommentRule.share_id == share_id,
                    m.ManagedCommentRule.user_id == user_id,
                )
            )
            rule.allowed = False
        elif change == "session":
            db_session.delete(db_session.get(m.UserSession, session_id))
        elif change == "generation":
            db_session.get(m.ManagedEntry, entry_id).collaboration_generation = uuid.uuid4()
        else:
            db_session.get(m.ManagedEntry, entry_id).deleted = True
        db_session.commit()
        return result

    monkeypatch.setattr(anchor_service, "validate", validate)
    with pytest.raises(HTTPException) as error:
        discussion_service.apply_command(db_session, token, payload)
    assert (
        error.value.status_code
        == {"permission": 403, "session": 401, "generation": 409, "entry": 404}[change]
    )
    assert count(db_session, m.DiscussionThread) == 0
    assert count(db_session, m.DiscussionReceipt) == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"request_sha256": "b" * 64},
        {"generation": str(uuid.uuid4())},
        {"expires_at_ms": 0},
        {"observed_snapshot_sha256": "wrong"},
        {"placements": [{"status": "attached", "from": 10, "to": 6}]},
    ],
)
def test_text_anchor_rejects_unbound_or_invalid_relay_responses(
    client, db_session, world, monkeypatch, patch
):
    enable_anchors(monkeypatch)
    relay_mock(
        monkeypatch,
        lambda request, response: (
            (200, {**response, **patch}) if "anchors" in request else (200, response)
        ),
    )
    send(client, world, text_command(world), expected=503)
    assert count(db_session, m.DiscussionThread) == count(db_session, m.DiscussionReceipt) == 0


def test_text_anchor_dependency_wait_retries_and_deleted_selection_is_preserved(
    client, db_session, world, monkeypatch
):
    enable_anchors(monkeypatch)
    state = {"waiting": True}

    def change(request, response):
        if "anchors" not in request:
            return 200, response
        if state["waiting"]:
            return 409, {"code": "dependency_not_ready"}
        return 200, {**response, "placements": [{"status": "unanchored"}]}

    relay_mock(monkeypatch, change)
    payload = text_command(world)
    failure = send(client, world, payload, expected=409)
    assert failure["error"]["message"]["code"] == "anchor_dependency_not_ready"
    assert count(db_session, m.DiscussionReceipt) == 0
    state["waiting"] = False
    result = send(client, world, payload)
    assert result["operation_id"] == payload["operation_id"]
    assert count(db_session, m.DiscussionThread) == 1


def test_text_anchor_expired_before_commit_rolls_back_all_discussion_state(
    db_session, world, monkeypatch
):
    enable_anchors(monkeypatch)
    payload = CommandEnvelope.model_validate(text_command(world))
    calls = []
    original = anchor_service.AnchorProof.check

    def check(self, envelope, generation):
        calls.append(True)
        if len(calls) == 2:
            anchor_service.fail(503, "anchor_validation_expired")
        original(self, envelope, generation)

    monkeypatch.setattr(anchor_service, "validate", proof)
    monkeypatch.setattr(anchor_service.AnchorProof, "check", check)
    with pytest.raises(HTTPException):
        discussion_service.apply_command(db_session, world.tokens["reviewer"], payload)
    assert len(calls) == 2
    assert (
        count(db_session, m.DiscussionThread)
        == count(db_session, m.DiscussionEvent)
        == count(db_session, m.DiscussionReceipt)
        == 0
    )


def test_text_anchor_envelope_limits_are_separate_and_disabled_by_default(
    client, db_session, world, monkeypatch
):
    payload = text_command(world)
    send(client, world, payload, expected=409)
    enable_anchors(monkeypatch)
    relay_mock(monkeypatch)
    payload["command"]["anchor"]["dependency"] = base64.b64encode(bytes(65536)).decode()
    assert len(json.dumps(payload).encode()) > 65536
    send(client, world, payload)
    payload["operation_id"] = str(uuid.uuid4())
    payload["command"]["anchor"]["dependency"] = base64.b64encode(bytes(65537)).decode()
    send(client, world, payload, expected=422)
    oversized_note = json.dumps(create(world)) + " " * 65536
    assert (
        client.post(
            "/v1/collaboration/commands",
            headers=headers(world.tokens["reviewer"]),
            content=oversized_note,
        ).status_code
        == 413
    )

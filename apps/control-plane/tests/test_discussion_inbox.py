import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_discussions import base, body, count, create, envelope, headers, send
from test_discussions import world as discussion_world  # noqa: F401

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_sessionmaker
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_inbox as inbox
from app.services import discussion_service
from app.workers import discussion_inbox_worker as worker


@pytest.fixture
def world(request, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_inbox_enabled", True)
    return request.getfixturevalue("discussion_world")


def mention(*users):
    value = body()
    value["blocks"][0]["children"].extend(
        {"type": "mention", "user_id": str(user.id)} for user in users
    )
    return value


def new_thread(client, w, *mentions):
    payload = create(w)
    payload["command"]["body"] = mention(*mentions)
    return payload, send(client, w, payload)


def reply(client, w, tid, *, actor="owner", mentions=()):
    return send(
        client,
        w,
        envelope(
            w,
            {
                "type": "reply.create",
                "thread_id": tid,
                "message_id": str(uuid.uuid4()),
                "body": mention(*mentions),
            },
        ),
        actor=actor,
    )


def listing(client, w, actor="owner", **params):
    response = client.get(
        f"/v1/collaboration/shares/{w.share.id}/inbox",
        params=params,
        headers=headers(w.tokens[actor]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def preference(client, w, tid, *, actor="owner", expected=200, **data):
    response = client.post(
        base(w) + f"/threads/{tid}/subscription", json=data, headers=headers(w.tokens[actor])
    )
    assert response.status_code == expected, response.text
    return response.json()


def mark(client, w, tid, version, action="read", *, actor="owner", expected=200):
    response = client.post(
        base(w) + f"/threads/{tid}/inbox",
        json={"seen_version": version, "action": action},
        headers=headers(w.tokens[actor]),
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_restore_guards_inbox_choices_and_exact_subscription_retries(client, db_session, world):
    w = world
    _, result = new_thread(client, w, w.owner)
    tid = result["thread_id"]
    assert inbox.run_batch() == 1
    original = listing(client, w)["items"][0]
    payload = {"expected_version": 0, "following": True, "muted": True}
    accepted = preference(client, w, tid, **payload)
    generation = uuid.uuid4()
    w.share.collaboration_restore_generation = generation
    db_session.commit()
    auth = headers(w.tokens["owner"])
    for old in (None, uuid.uuid4()):
        stale = dict(auth)
        if old:
            stale["X-Relay-Restore-Generation"] = str(old)
        # A prior exact receipt/version match must not bypass the restore guard.
        response = client.post(
            base(w) + f"/threads/{tid}/subscription", json=payload, headers=stale
        )
        assert response.status_code == 409 and "restore_generation_changed" in response.text
        for action in ("read", "unread", "dismiss"):
            response = client.post(
                base(w) + f"/threads/{tid}/inbox",
                json={"seen_version": original["version"], "action": action},
                headers=stale,
            )
            assert response.status_code == 409 and "restore_generation_changed" in response.text
    # Read-only retrieval still works without a mutation generation.
    current = client.get(base(w) + f"/threads/{tid}/subscription", headers=auth)
    assert current.status_code == 200 and current.json() == accepted
    item = listing(client, w, include_muted=True)["items"][0]
    assert item["read_version"] == 0 and item["dismissed_version"] == 0
    auth["X-Relay-Restore-Generation"] = str(generation)
    response = client.post(base(w) + f"/threads/{tid}/subscription", json=payload, headers=auth)
    assert response.status_code == 200 and response.json() == accepted
    response = client.post(
        base(w) + f"/threads/{tid}/inbox",
        json={"seen_version": original["version"], "action": "dismiss"},
        headers=auth,
    )
    assert (
        response.status_code == 200 and response.json()["dismissed_version"] == original["version"]
    )


def test_mentions_commit_with_message_and_exact_retry_never_duplicates(client, db_session, world):
    w = world
    payload, result = new_thread(client, w, w.owner, w.owner, w.reviewer)
    assert count(db_session, m.DiscussionNotification) == 1
    assert listing(client, w)["items"] == []
    assert send(client, w, payload) == result
    assert count(db_session, m.DiscussionNotification) == 1
    assert inbox.run_batch() == 1
    items = listing(client, w)["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "mention"
    assert items[0]["version"] == 1 and items[0]["unread"]
    assert items[0]["subscription"] == {"following": False, "muted": False, "version": 0}
    assert listing(client, w, actor="reviewer")["items"] == []
    assert inbox.run_batch() == 0


def test_failed_transaction_does_not_leave_candidate_or_comment(
    client, db_session, world, monkeypatch
):
    w = world
    payload = create(w)
    payload["command"]["body"] = mention(w.owner)
    original = inbox.enqueue

    def fail(*args):
        original(*args)
        args[0].flush()
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(inbox, "enqueue", fail)
    with pytest.raises(RuntimeError):
        discussion_service.apply_command(
            db_session, w.tokens["reviewer"], CommandEnvelope.model_validate(payload)
        )
    assert count(db_session, m.DiscussionNotification) == 0
    assert count(db_session, m.DiscussionMessage) == 0
    assert count(db_session, m.DiscussionReceipt) == 0


def test_participants_follow_by_default_and_mention_wins_over_reply(client, db_session, world):
    w = world
    _, result = new_thread(client, w)
    tid = result["thread_id"]
    reply(client, w, tid, mentions=(w.reviewer,))
    inbox.run_batch()
    item = listing(client, w, actor="reviewer")["items"][0]
    assert item["reason"] == "mention" and item["version"] == 1
    assert item["subscription"]["following"]
    reply(client, w, tid)
    inbox.run_batch()
    item = listing(client, w, actor="reviewer")["items"][0]
    assert item["reason"] == "reply" and item["version"] == 2
    assert count(db_session, m.DiscussionInboxItem) == 1


def test_reader_can_subscribe_without_comment_permission_and_retry(client, world):
    w = world
    _, result = new_thread(client, w)
    tid = result["thread_id"]
    state = preference(
        client, w, tid, actor="reader", expected_version=0, following=True, muted=False
    )
    assert state == {"version": 1, "following": True, "muted": False}
    assert (
        preference(client, w, tid, actor="reader", expected_version=0, following=True, muted=False)
        == state
    )
    preference(
        client,
        w,
        tid,
        actor="reader",
        expected_version=0,
        following=False,
        muted=False,
        expected=409,
    )
    reply(client, w, tid)
    inbox.run_batch()
    assert len(listing(client, w, actor="reader")["items"]) == 1


def test_explicit_unsubscribe_survives_participation_but_mentions_still_deliver(client, world):
    w = world
    _, result = new_thread(client, w)
    tid = result["thread_id"]
    preference(client, w, tid, actor="reviewer", expected_version=0, following=False, muted=False)
    reply(client, w, tid, actor="reviewer")
    reply(client, w, tid)
    inbox.run_batch()
    assert listing(client, w, actor="reviewer")["items"] == []
    reply(client, w, tid, mentions=(w.reviewer,))
    inbox.run_batch()
    item = listing(client, w, actor="reviewer")["items"][0]
    assert item["reason"] == "mention" and not item["subscription"]["following"]


def test_mute_hides_existing_and_suppresses_pending_and_new_notifications(
    client, db_session, world
):
    w = world
    _, result = new_thread(client, w, w.owner)
    tid = result["thread_id"]
    inbox.run_batch()
    reply(client, w, tid, actor="reviewer", mentions=(w.owner,))
    preference(client, w, tid, expected_version=0, following=True, muted=True)
    inbox.run_batch()
    assert listing(client, w)["items"] == []
    assert listing(client, w, include_muted=True)["items"][0]["version"] == 1
    reply(client, w, tid, actor="reviewer", mentions=(w.owner,))
    assert count(db_session, m.DiscussionNotification) == 2
    preference(client, w, tid, expected_version=1, following=True, muted=False)
    assert listing(client, w)["items"][0]["version"] == 1


def test_only_newly_added_mentions_notify_on_edits(client, db_session, world):
    w = world
    _, result = new_thread(client, w, w.owner)
    inbox.run_batch()
    for revision, users in [(1, (w.owner,)), (2, (w.owner, w.reader)), (3, (w.owner, w.reader))]:
        send(
            client,
            w,
            envelope(
                w,
                {
                    "type": "message.edit",
                    "thread_id": result["thread_id"],
                    "message_id": result["message_id"],
                    "expected_revision": revision,
                    "body": mention(*users),
                },
            ),
        )
        inbox.run_batch()
    assert count(db_session, m.DiscussionNotification) == 2
    assert listing(client, w)["items"][0]["version"] == 1
    reader = listing(client, w, actor="reader")["items"][0]
    assert reader["version"] == 1 and reader["message_revision"] == 4


def test_new_reply_and_late_projection_stay_unread_after_seen_version(client, world):
    w = world
    _, result = new_thread(client, w, w.owner)
    tid = result["thread_id"]
    factory = get_sessionmaker()
    oldest = inbox.claim(factory)
    reply(client, w, tid, actor="reviewer", mentions=(w.owner,))
    newest = inbox.claim(factory)
    assert inbox.project(factory, newest)
    assert mark(client, w, tid, 1)["unread"] is False
    # A preempted worker resumes an older event after the newer one was read.
    assert inbox.project(factory, oldest)
    assert mark(client, w, tid, 1)["unread"] is True
    item = listing(client, w)["items"][0]
    assert item["version"] == 2 and item["last_sequence"] == newest[1]
    mark(client, w, tid, 3, expected=409)
    assert mark(client, w, tid, 2)["unread"] is False
    assert listing(client, w, unread_only=True)["items"] == []
    assert mark(client, w, tid, 2, "unread")["unread"] is True


def test_dismissal_does_not_hide_a_concurrent_reply_and_can_mark_unread(client, world):
    w = world
    _, result = new_thread(client, w, w.owner)
    tid = result["thread_id"]
    inbox.run_batch()
    mark(client, w, tid, 1, "dismiss")
    assert listing(client, w)["items"] == []
    assert len(listing(client, w, include_dismissed=True)["items"]) == 1
    reply(client, w, tid, actor="reviewer", mentions=(w.owner,))
    inbox.run_batch()
    mark(client, w, tid, 1, "dismiss")
    assert listing(client, w)["items"][0]["unread"]
    mark(client, w, tid, 1, "unread", expected=409)
    mark(client, w, tid, 2, "dismiss")
    mark(client, w, tid, 2, "unread")
    assert len(listing(client, w)["items"]) == 1


def test_lease_expiration_and_stale_worker_cannot_duplicate_projection(client, db_session, world):
    w = world
    new_thread(client, w, w.owner)
    factory = get_sessionmaker()
    old = inbox.claim(factory)
    assert inbox.claim(factory) is None
    job = db_session.get(m.DiscussionNotification, old[:3])
    job.lease_until = security.utcnow() - timedelta(seconds=1)
    db_session.commit()
    new = inbox.claim(factory)
    assert old[:3] == new[:3] and old[3] != new[3]
    assert not inbox.project(factory, old)
    assert inbox.project(factory, new)
    assert not inbox.project(factory, new)
    assert listing(client, w)["items"][0]["version"] == 1


@pytest.mark.parametrize("when", ["before_projection", "after_projection"])
@pytest.mark.parametrize("revoke", ["rule", "membership", "user", "entry", "thread", "message"])
def test_revocation_and_removal_disclose_no_items(client, db_session, world, when, revoke):
    w = world
    payload = create(w)
    payload["command"]["body"] = mention(w.reviewer)
    result = send(client, w, payload, actor="owner")
    if when == "after_projection":
        inbox.run_batch()
        assert len(listing(client, w, actor="reviewer")["items"]) == 1
    if revoke == "rule":
        db_session.scalar(
            select(m.ManagedRule).where(m.ManagedRule.user_id == w.reviewer.id)
        ).permission = "deny"
    elif revoke == "membership":
        db_session.delete(
            db_session.scalar(select(m.ShareMember).where(m.ShareMember.user_id == w.reviewer.id))
        )
    elif revoke == "user":
        w.reviewer.is_active = False
    elif revoke == "entry":
        w.entry.deleted = True
    elif revoke == "thread":
        db_session.get(m.DiscussionThread, uuid.UUID(result["thread_id"])).deleted = True
    else:
        db_session.get(m.DiscussionMessage, uuid.UUID(result["message_id"])).deleted = True
    db_session.commit()
    inbox.run_batch()
    response = client.get(
        f"/v1/collaboration/shares/{w.share.id}/inbox", headers=headers(w.tokens["reviewer"])
    )
    if revoke in {"membership", "user"}:
        assert response.status_code in {401, 403, 404}
    else:
        assert response.json() == {"items": [], "next_cursor": None}
    assert w.entry.path not in response.text
    if when == "before_projection":
        assert count(db_session, m.DiscussionInboxItem) == 0


def test_removed_mention_before_projection_is_suppressed(client, world):
    w = world
    _, result = new_thread(client, w, w.owner)
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": result["thread_id"],
                "message_id": result["message_id"],
                "expected_revision": 1,
                "body": body("Corrected without mention"),
            },
        ),
    )
    inbox.run_batch()
    assert listing(client, w)["items"] == []


def test_activity_pagination_is_account_and_filter_bound(client, world):
    w = world
    tids = [new_thread(client, w, w.owner)[1]["thread_id"] for _ in range(3)]
    inbox.run_batch()
    first = listing(client, w, limit=1)
    second = listing(client, w, limit=1, cursor=first["next_cursor"])
    third = listing(client, w, limit=1, cursor=second["next_cursor"])
    assert [page["items"][0]["thread_id"] for page in (first, second, third)] == tids[::-1]
    assert third["next_cursor"] is None
    for actor, extra in [("reviewer", {}), ("owner", {"unread_only": True})]:
        response = client.get(
            f"/v1/collaboration/shares/{w.share.id}/inbox",
            params={"cursor": first["next_cursor"], **extra},
            headers=headers(w.tokens[actor]),
        )
        assert response.status_code == 409


def test_reactions_and_resolution_generate_no_notification(client, db_session, world):
    w = world
    _, result = new_thread(client, w)
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "reaction.set",
                "thread_id": result["thread_id"],
                "message_id": result["message_id"],
                "emoji": "heart",
                "present": True,
            },
        ),
        actor="owner",
    )
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "thread.state",
                "thread_id": result["thread_id"],
                "expected_state_revision": 1,
                "state": "resolved",
            },
        ),
        actor="owner",
    )
    assert count(db_session, m.DiscussionNotification) == 0


def test_disabled_inbox_preserves_pending_work_and_advertises_capability(
    client, db_session, world, monkeypatch
):
    w = world
    new_thread(client, w, w.owner)
    monkeypatch.setattr(get_settings(), "collaboration_inbox_enabled", False)
    assert inbox.run_batch() == 0
    assert count(db_session, m.DiscussionNotification) == 1
    response = client.get(
        f"/v1/collaboration/shares/{w.share.id}/inbox", headers=headers(w.tokens["owner"])
    )
    assert response.status_code == 404
    response = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(w.share.id)},
        headers=headers(w.tokens["owner"]),
    )
    assert response.json()["features"]["discussion_inbox"] is False
    monkeypatch.setattr(get_settings(), "collaboration_inbox_enabled", True)
    assert inbox.run_batch() == 1


def test_worker_shutdown_drains_the_active_database_thread(monkeypatch):
    entered, finished = threading.Event(), threading.Event()

    def batch(stop):
        entered.set()
        assert stop.wait(5)
        finished.set()

    monkeypatch.setattr(worker, "run_batch", batch)

    async def exercise():
        stop = threading.Event()
        task = asyncio.create_task(worker.finish_batch(stop))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stop.is_set() and finished.is_set()

    asyncio.run(exercise())


def test_deleting_latest_notification_keeps_previous_readable_activity(client, world):
    w = world
    _, first = new_thread(client, w, w.owner)
    tid = first["thread_id"]
    inbox.run_batch()
    second = reply(client, w, tid, actor="reviewer", mentions=(w.owner,))
    inbox.run_batch()
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.delete",
                "thread_id": tid,
                "message_id": second["message_id"],
                "expected_revision": 1,
            },
        ),
    )
    item = listing(client, w)["items"][0]
    assert item["message_id"] == first["message_id"]
    assert item["version"] == 2


def test_removing_mention_from_pending_reply_keeps_subscriber_notification(client, world):
    w = world
    _, first = new_thread(client, w)
    second = reply(client, w, first["thread_id"], mentions=(w.reviewer,))
    send(
        client,
        w,
        envelope(
            w,
            {
                "type": "message.edit",
                "thread_id": first["thread_id"],
                "message_id": second["message_id"],
                "expected_revision": 1,
                "body": body("Updated reply"),
            },
        ),
        actor="owner",
    )
    inbox.run_batch()
    item = listing(client, w, actor="reviewer")["items"][0]
    assert item["reason"] == "reply" and item["version"] == 1


@pytest.mark.parametrize(
    "action,version", [("read", True), ("read", "1"), ("read", 0), ("read", 1.5), ("surprise", 1)]
)
def test_invalid_read_updates_fail_without_echoing_input(client, world, action, version):
    w = world
    _, result = new_thread(client, w, w.owner)
    inbox.run_batch()
    response = client.post(
        base(w) + f"/threads/{result['thread_id']}/inbox",
        json={"seen_version": version, "action": action},
        headers=headers(w.tokens["owner"]),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == {"code": "invalid_command"}
    assert set(error) == {"code", "message", "request_id"}
    assert listing(client, w)["items"][0]["unread"]

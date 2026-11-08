import importlib
import json
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_base_discussions import base as base_fixture
from test_base_discussions import command
from test_base_discussions import targets as targets_fixture
from test_discussions import base as discussion_path
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_moderation as moderation

world = discussion_world
base = base_fixture
targets = targets_fixture


@pytest.fixture(autouse=True)
def enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_moderation_recovery_enabled", True)


def remove(client, w, *, thread=False):
    made = send(client, w, create(w, "Retained private history"))
    if thread:
        payload = dict(
            type="thread.remove",
            thread_id=made["thread_id"],
            expected_state_revision=1,
            reason="Synthetic thread removal",
        )
    else:
        payload = dict(
            type="message.delete",
            thread_id=made["thread_id"],
            message_id=made["message_id"],
            expected_revision=1,
            reason="Synthetic message removal",
        )
    send(client, w, envelope(w, payload), actor="owner")
    return made


def path(w, made):
    return (
        discussion_path(w)
        + f"/threads/{made['thread_id']}/messages/{made['message_id']}/moderation-history"
    )


def recover(client, w, made, *, actor="owner", expected=200, **overrides):
    response = client.post(
        path(w, made),
        headers={**headers(w.tokens[actor]), "Content-Type": "application/json"},
        content=json.dumps(
            {"protocol_version": 1, "reason": "Investigate synthetic removal", **overrides}
        ),
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.headers["cache-control"] == "no-store"
    else:
        assert "Retained private" not in response.text
    return response.json()


@pytest.mark.parametrize("thread", [False, True])
def test_explicit_moderator_read_audits_each_page_before_disclosure(
    client, db_session, world, thread
):
    w = world
    made = remove(client, w, thread=thread)
    before = db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog))
    page = recover(client, w, made, limit=1)
    assert page["revisions"][0]["body"] == body("Retained private history")
    assert page["thread_removed"] is thread and page["message_removed"] is not thread
    assert page["has_more"] is not thread
    audit = db_session.get(m.AuditLog, uuid.UUID(page["audit_id"]))
    assert audit and audit.action == m.AuditAction.DISCUSSION_RECOVERY_READ
    assert audit.actor_user_id == w.owner.id and audit.target_share_id == w.share.id
    assert audit.details["message_id"] == made["message_id"]
    assert audit.details["reason"] == "Investigate synthetic removal"
    assert audit.details["revisions"] == [1]
    assert "Retained private" not in str(audit.details)
    row = db_session.get(
        m.DiscussionThread if thread else m.DiscussionMessage,
        uuid.UUID(made["thread_id" if thread else "message_id"]),
    )
    assert row.deleted_at is not None and row.content_purged_at is None
    expected_until = moderation.aware(row.deleted_at) + timedelta(days=30)
    assert page["recoverable_until"] == expected_until.isoformat()
    second = recover(client, w, made, after=page["next_after"], limit=1)
    assert second["audit_id"] != page["audit_id"]
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog)) == before + 2
    if not thread:
        assert second["revisions"][0]["body"] is None
        assert second["revisions"][0]["reason"] == "Synthetic message removal"
    else:
        assert (
            second["revisions"] == []
            and second["thread_removal_reason"] == "Synthetic thread removal"
        )
    # Audit permission does not reopen an ordinary route.
    assert (
        client.get(
            path(w, made).replace("moderation-history", "history"),
            headers=headers(w.tokens["owner"]),
        ).status_code
        == 404
    )


@pytest.mark.parametrize("actor", ["reader", "reviewer"])
def test_authorship_or_read_permission_never_grants_recovery(client, db_session, world, actor):
    made = remove(client, world)
    before = db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog))
    recover(client, world, made, actor=actor, expected=403)
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog)) == before


@pytest.mark.parametrize(
    "case",
    [
        "live",
        "expired",
        "thread_expired",
        "missing_clock",
        "purged",
        "revoked_session",
        "entry_deleted",
        "mixed_message",
        "mixed_thread",
    ],
)
def test_recovery_rechecks_identity_session_and_retention(client, db_session, world, case):
    w = world
    made = (
        send(client, w, create(w))
        if case == "live"
        else remove(client, w, thread=case == "thread_expired")
    )
    thread = db_session.get(m.DiscussionThread, uuid.UUID(made["thread_id"]))
    message = db_session.get(m.DiscussionMessage, uuid.UUID(made["message_id"]))
    expected = 404
    if case == "expired":
        message.deleted_at = security.utcnow() - timedelta(days=30, seconds=1)
    elif case == "thread_expired":
        thread.deleted_at = security.utcnow() - timedelta(days=30, seconds=1)
    elif case == "missing_clock":
        message.deleted_at = None
    elif case == "purged":
        message.content_purged_at = security.utcnow()
    elif case == "revoked_session":
        db_session.delete(db_session.get(m.UserSession, w.sessions["owner"]))
        expected = 401
    elif case == "entry_deleted":
        w.entry.deleted = True
    elif case == "mixed_message":
        made["message_id"] = str(uuid.uuid4())
    elif case == "mixed_thread":
        made["thread_id"] = str(uuid.uuid4())
    db_session.commit()
    before = db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog))
    recover(client, w, made, expected=expected)
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog)) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": ""},
        {"reason": " \n "},
        {"reason": "x" * 513},
        {"reason": 123},
        {"reason": "bad\x00value"},
        {"reason": "\ud800"},
        {"after": True},
        {"limit": 51},
        {"limit": 0},
        {"protocol_version": True},
        {"unknown": "private extra"},
    ],
)
def test_recovery_requires_explicit_bounded_plain_reason(client, world, overrides):
    made = remove(client, world)
    recover(client, world, made, expected=422, **overrides)


def test_recovery_is_independently_disabled_and_never_a_get(client, world, monkeypatch):
    made = remove(client, world)
    assert client.get(path(world, made), headers=headers(world.tokens["owner"])).status_code == 405
    monkeypatch.setattr(get_settings(), "collaboration_moderation_recovery_enabled", False)
    recover(client, world, made, expected=404)


def test_failed_audit_commit_discloses_no_content_and_rolls_back(
    client, db_session, world, monkeypatch
):
    made = remove(client, world)
    before = db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog))
    with monkeypatch.context() as patch:

        def fail(self):
            raise OperationalError("synthetic audit commit", {}, None)

        patch.setattr(Session, "commit", fail)
        recover(client, world, made, expected=503)
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog)) == before
    recover(client, world, made)


def test_concurrency_slot_refuses_without_leaking_or_leaking_slot(client, world):
    made = remove(client, world)
    assert moderation.slots.acquire(False) and moderation.slots.acquire(False)
    try:
        recover(client, world, made, expected=503)
    finally:
        moderation.slots.release()
        moderation.slots.release()
    recover(client, world, made)


def test_migration_backfills_removal_clocks_without_purging_and_round_trips():
    module = importlib.import_module(
        "app.db.migrations.versions.202611070003_discussion_moderation_recovery"
    )
    engine = sa.create_engine("sqlite://")
    schema = sa.MetaData()
    uid = sa.Uuid()
    threads = sa.Table(
        "discussion_threads",
        schema,
        sa.Column("id", uid, primary_key=True),
        sa.Column("scope_id", uid),
        sa.Column("activity_sequence", sa.BigInteger),
        sa.Column("deleted", sa.Boolean),
    )
    messages = sa.Table(
        "discussion_messages",
        schema,
        sa.Column("id", uid, primary_key=True),
        sa.Column("revision", sa.Integer),
        sa.Column("deleted", sa.Boolean),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
    )
    revisions = sa.Table(
        "discussion_message_revisions",
        schema,
        sa.Column("message_id", uid),
        sa.Column("revision", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    events = sa.Table(
        "discussion_events",
        schema,
        sa.Column("scope_id", uid),
        sa.Column("sequence", sa.BigInteger),
        sa.Column("kind", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    past = security.utcnow() - timedelta(days=10)
    one, two, scope, live = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as db:
            schema.create_all(db)
            db.execute(
                threads.insert(),
                [
                    {"id": one, "scope_id": scope, "activity_sequence": 3, "deleted": True},
                    {"id": two, "scope_id": scope, "activity_sequence": 4, "deleted": True},
                    {"id": live, "scope_id": scope, "activity_sequence": 2, "deleted": False},
                ],
            )
            db.execute(
                events.insert(),
                {"scope_id": scope, "sequence": 3, "kind": "thread.remove", "created_at": past},
            )
            db.execute(
                messages.insert(), {"id": one, "revision": 2, "deleted": True, "edited_at": None}
            )
            db.execute(revisions.insert(), {"message_id": one, "revision": 2, "created_at": past})
            with Operations.context(MigrationContext.configure(db)):
                module.upgrade()
                rows = db.execute(
                    sa.text(
                        "SELECT deleted_at, content_purged_at FROM discussion_threads "
                        "ORDER BY activity_sequence"
                    )
                ).all()
                assert rows[0] == (None, None)
                assert rows[1][0].startswith(past.strftime("%Y-%m-%d")) and rows[1][1] is None
                assert rows[2][0] and rows[2][1] is None
                assert db.scalar(sa.text("SELECT deleted_at FROM discussion_messages")).startswith(
                    past.strftime("%Y-%m-%d")
                )
                module.downgrade()
                assert "deleted_at" not in {
                    c["name"] for c in sa.inspect(db).get_columns("discussion_threads")
                }
                assert db.scalar(sa.select(sa.func.count()).select_from(threads)) == 3
                module.upgrade()
    finally:
        engine.dispose()


from test_discussion_pictures import publish, raster, upload
from test_discussion_pictures import storage as picture_storage

storage = picture_storage


def picture_target(client, w):
    pid, _ = upload(client, w, raster())
    _, made = publish(client, w, pid)
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="message.delete",
                thread_id=made["thread_id"],
                message_id=made["message_id"],
                expected_revision=1,
            ),
        ),
    )
    return made, pid


def picture_recover(client, w, made, pid, suffix="", actor="owner", expected=200):
    url = path(w, made).replace("moderation-history", "moderation-pictures/" + str(pid)) + suffix
    response = client.post(
        url, headers=headers(w.tokens[actor]), json={"reason": "Review removed picture"}
    )
    assert response.status_code == expected, (
        response.text if expected != 200 else "picture response"
    )
    return response


def test_recovery_pictures_require_exact_message_reference_and_commit_audit(
    client, db_session, world, storage
):
    w = world
    made, pid = picture_target(client, w)
    metadata = picture_recover(client, w, made, pid).json()
    assert metadata["picture"]["state"] == "attached" and "key" not in str(metadata["picture"])
    assert db_session.get(m.AuditLog, uuid.UUID(metadata["audit_id"]))
    for variant in ("content", "thumbnail"):
        response = picture_recover(client, w, made, pid, "/" + variant)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "image/png" and response.content.startswith(
            b"\x89PNG"
        )
        audit = db_session.get(m.AuditLog, uuid.UUID(response.headers["x-discussion-audit-id"]))
        assert audit.details["picture_id"] == str(pid) and audit.details["variant"] == variant
        assert audit.details["revisions"] == [1]
    other, _ = upload(client, w, raster())
    picture_recover(client, w, made, other, expected=404)
    for actor in ("reader", "reviewer"):
        picture_recover(client, w, made, pid, expected=403, actor=actor)
        picture_recover(client, w, made, pid, "/content", expected=403, actor=actor)
    picture_recover(client, w, made, pid, "/original", expected=404)


@pytest.mark.parametrize(
    "change", ["expiration", "purge", "session", "flag", "picture", "audit_failure"]
)
def test_recovery_picture_rechecks_after_storage_io_and_never_discloses_without_audit(
    client, db_session, world, storage, monkeypatch, change
):
    w = world
    made, pid = picture_target(client, w)
    before = db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog))

    def while_reading():
        message = db_session.get(m.DiscussionMessage, uuid.UUID(made["message_id"]))
        if change == "expiration":
            message.deleted_at = security.utcnow() - timedelta(days=31)
        elif change == "purge":
            message.content_purged_at = security.utcnow()
        elif change == "session":
            db_session.delete(db_session.get(m.UserSession, w.sessions["owner"]))
        elif change == "flag":
            monkeypatch.setattr(get_settings(), "collaboration_moderation_recovery_enabled", False)
        elif change == "picture":
            picture = db_session.get(m.DiscussionPicture, uuid.UUID(pid))
            picture.state = "expired"
        db_session.commit()
        if change == "audit_failure":

            def fail(self):
                raise OperationalError("synthetic audit failure", {}, None)

            monkeypatch.setattr(Session, "commit", fail)

    storage.before_get = while_reading
    expected = 401 if change == "session" else 503 if change == "audit_failure" else 404
    picture_recover(client, w, made, pid, "/content", expected=expected)
    assert storage.last_response.closed and storage.last_response.released
    assert db_session.scalar(sa.select(sa.func.count()).select_from(m.AuditLog)) == before


def test_recovery_keeps_current_bases_record_authorization(client, db_session, targets):
    t = targets
    made = send(client, t.w, command(t, "cell"))
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="message.delete",
                thread_id=made["thread_id"],
                message_id=made["message_id"],
                expected_revision=1,
            ),
        ),
    )
    recover(client, t.w, made)
    t.record.deleted = True
    db_session.commit()
    recover(client, t.w, made, expected=404)


def test_recovery_page_is_bounded_and_audit_lists_only_disclosed_revisions(
    client, db_session, world
):
    made = remove(client, world)
    mid = uuid.UUID(made["message_id"])
    message = db_session.get(m.DiscussionMessage, mid)
    for revision in range(3, 63):
        rich = body("A" * 8000)
        rich["blocks"][0]["children"].append({"type": "text", "text": "B" * 8000, "marks": []})
        db_session.add(m.DiscussionMessageRevision(message_id=mid, revision=revision, body=rich))
    message.revision = 62
    db_session.commit()
    page = recover(client, world, made, after=2, limit=50)
    assert 1 < len(page["revisions"]) < 50 and page["has_more"]
    assert len(json.dumps(page).encode()) < 512 * 1024
    audit = db_session.get(m.AuditLog, uuid.UUID(page["audit_id"]))
    assert audit.details["revisions"] == [row["revision"] for row in page["revisions"]]
    next_page = recover(client, world, made, after=page["next_after"], limit=50)
    assert next_page["revisions"][0]["revision"] == page["next_after"] + 1


def test_earliest_removal_clock_and_configured_retention_apply(
    client, db_session, world, monkeypatch
):
    made = remove(client, world)
    message = db_session.get(m.DiscussionMessage, uuid.UUID(made["message_id"]))
    message.deleted_at = security.utcnow() - timedelta(days=2)
    db_session.commit()
    send(
        client,
        world,
        envelope(
            world,
            dict(
                type="thread.remove",
                thread_id=made["thread_id"],
                expected_state_revision=1,
                reason="Later thread removal",
            ),
        ),
        actor="owner",
    )
    recover(client, world, made)
    monkeypatch.setattr(get_settings(), "collaboration_removed_content_retention_days", 1)
    recover(client, world, made, expected=404)

"""Source-backed group capture and per-thread witness authorization."""

import base64
import copy
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_base_discussions import base, context, deny_record, get, targets, world
from test_base_target_definitions import catalog, configure
from test_bases_registry import path
from test_discussion_pictures import get_picture, picture_body, raster, upload
from test_discussion_pictures import storage as storage_fixture
from test_discussions import body, create, envelope, headers, send

from app.core.config import get_settings
from app.db import models as m
from app.services import anchor_service, base_groups, base_source_service, discussion_service

world, base, targets = world, base, targets
storage = storage_fixture


@pytest.fixture
def groups(client, targets, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "collaboration_bases_definition_targets_enabled", True)
    monkeypatch.setattr(settings, "collaboration_bases_groups_enabled", True)
    monkeypatch.setattr(
        settings, "collaboration_anchor_relay_token", SecretStr("test-only-source-token")
    )
    t = targets
    configure(client, t, groupBy={"property": "status", "direction": "ASC"})
    state = SimpleNamespace(
        source="---\nstatus: Done\n---\n", calls=0, before=None, db=None, expired=False
    )
    original = discussion_service.context

    def track(db, *args, **kwargs):
        state.db = db
        return original(db, *args, **kwargs)

    def observe(share_id, entry_id, generation, dependency):
        assert not state.db.in_transaction(), "Private source I/O holds a SQL transaction"
        state.calls += 1
        if state.before:
            callback, state.before = state.before, None
            callback()
        return base_source_service.SourceObservation(
            identity_sha256=base_source_service.source_identity(
                share_id, entry_id, generation, dependency
            ),
            observed_snapshot_sha256=hashlib.sha256(dependency.encode()).hexdigest(),
            source_prefix_sha256=hashlib.sha256(state.source.encode()).hexdigest(),
            source_complete=True,
            expires_at_ms=anchor_service.now_ms() + (-1 if state.expired else 15000),
            deadline=time.monotonic() + 12,
            source_prefix=state.source,
        )

    monkeypatch.setattr(discussion_service, "context", track)
    monkeypatch.setattr(base_source_service, "observe", observe)
    t.source = state
    return t


def request(client, t, *, record=None, value=None):
    captured = context(client, t.w, t.captured["view_id"], record=record or t.record)
    page = catalog(client, t)
    grouping = next(item for item in page["targets"] if item["kind"] == "group")
    return {
        **{k: captured[k] for k in ("definition_id", "view_id", "generation")},
        "registry_revision": page["registry_revision"],
        "group_definition_id": grouping["id"],
        "group_definition_revision": grouping["revision"],
        "record_id": captured["record_id"],
        "record_generation": captured["record_generation"],
        "operation_id": str(uuid.uuid4()),
        "dependency": "AA==",
        "time_zone": "UTC",
        "value": value if value is not None else {"type": "text", "value": "Done"},
    }


def capture(client, t, payload, *, actor="reviewer", expected=200):
    response = client.post(
        path(t.w) + "/group-targets", headers=headers(t.w.tokens[actor]), json=payload
    )
    assert response.status_code == expected, response.text
    return response.json()


def thread(t, captured):
    result = create(t.w)
    result["command"]["anchor"] = captured["anchor"]
    return result


def counts(db):
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (m.BaseGroupValue, m.BaseGroupWitness, m.BaseGroupCaptureReceipt)
    )


def add_record(db, t, path="Notes/Visible.md"):
    record = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=t.w.share.id,
        path=path,
        record={"type": "markdown", "version": 0},
        collaboration_generation=uuid.uuid4(),
        deleted=False,
    )
    db.add(record)
    db.commit()
    return record


@pytest.fixture
def pictures(groups, request):
    return request.getfixturevalue("storage")


def test_comment_only_capture_is_idempotent_and_requires_read_not_comment_on_source(
    client, db_session, groups
):
    t = groups
    payload = request(client, t)
    deny_record(db_session, t, read=False)
    captured = capture(client, t, payload)
    assert capture(client, t, payload) == captured and t.source.calls == 1
    accepted = send(client, t.w, thread(t, captured))
    assert (
        get(client, t, f"/threads/{accepted['thread_id']}")["anchor"]["target"]
        == captured["anchor"]["target"]
    )
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="reply.create",
                thread_id=accepted["thread_id"],
                message_id=str(uuid.uuid4()),
                body=body(),
            ),
        ),
    )
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="thread.state",
                thread_id=accepted["thread_id"],
                state="resolved",
                expected_state_revision=1,
            ),
        ),
    )
    capture(client, t, payload, actor="reader", expected=403)
    assert counts(db_session) == (1, 1, 1)


def test_matching_visible_group_never_unlocks_a_protected_threads_witness(
    client, db_session, groups
):
    t = groups
    original_request = request(client, t)
    original = capture(client, t, original_request)
    original_command = thread(t, original)
    accepted = send(client, t.w, original_command)
    visible = add_record(db_session, t)
    other = capture(client, t, request(client, t, record=visible))
    left, right = original["anchor"]["target"], other["anchor"]["target"]
    assert left["group_id"] == right["group_id"]
    assert left["witness_id"] != right["witness_id"]
    accessible = send(client, t.w, thread(t, other))
    deny_record(db_session, t)
    for suffix in (
        f"/threads/{accepted['thread_id']}",
        f"/threads/{accepted['thread_id']}/state",
        f"/threads/{accepted['thread_id']}/messages/{accepted['message_id']}/history",
    ):
        get(client, t, suffix, expected=404)
    send(client, t.w, original_command, expected=404)
    capture(client, t, original_request, expected=404)
    send(client, t.w, thread(t, original), expected=404)
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="reaction.set",
                thread_id=accepted["thread_id"],
                message_id=accepted["message_id"],
                emoji="heart",
                present=True,
            ),
        ),
        expected=404,
    )
    assert [
        row["id"] for row in get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]
    ] == [accessible["thread_id"]]
    events = get(client, t, "/changes", view_id=t.captured["view_id"])["events"]
    assert all(event["data"]["thread_id"] == accessible["thread_id"] for event in events)
    get(client, t, f"/threads/{accepted['thread_id']}", actor="owner")


@pytest.mark.parametrize("change", ["read", "delete", "generation", "source_type", "share"])
def test_capture_rechecks_source_after_private_io_and_never_persists_on_failure(
    client, db_session, groups, change
):
    t = groups
    payload = request(client, t)

    def revoke():
        if change == "read":
            deny_record(db_session, t)
            return
        db_session.refresh(t.record)
        if change == "delete":
            t.record.deleted = True
        elif change == "generation":
            t.record.collaboration_generation = uuid.uuid4()
        elif change == "source_type":
            t.record.record = {"type": "image"}
        else:
            member = db_session.scalar(
                select(m.ShareMember).where(
                    m.ShareMember.share_id == t.w.share.id,
                    m.ShareMember.user_id == t.w.reviewer.id,
                )
            )
            db_session.delete(member)
        db_session.commit()

    t.source.before = revoke
    capture(client, t, payload, expected=409 if change in {"generation", "source_type"} else 404)
    assert counts(db_session) == (0, 0, 0)


def test_capture_rejects_source_move_expiry_or_value_mismatch(client, db_session, groups):
    t = groups
    payload = request(client, t)

    def move():
        t.record.path = "Notes/Moved.md"
        db_session.commit()

    t.source.before = move
    assert "base_group_source_changed" in str(capture(client, t, payload, expected=409))
    t.source.expired = True
    capture(client, t, payload, expected=503)
    t.source.expired = False
    t.source.source = "---\nstatus: Todo\n---\n"
    assert "base_group_value_changed" in str(capture(client, t, payload, expected=409))
    assert counts(db_session) == (0, 0, 0)


def test_membership_changes_preserve_identity_and_historical_thread_but_lineage_loss_hides_it(
    client, db_session, groups
):
    t = groups
    first = capture(client, t, request(client, t))
    accepted = send(client, t.w, thread(t, first))
    t.source.source = "---\nstatus: Todo\n---\n"
    changed = capture(client, t, request(client, t, value={"type": "text", "value": "Todo"}))
    assert first["anchor"]["target"]["group_id"] != changed["anchor"]["target"]["group_id"]
    get(client, t, f"/threads/{accepted['thread_id']}")
    t.source.source = "---\nstatus: Done\n---\nA different body."
    returned = capture(client, t, request(client, t))
    assert first["anchor"]["target"] == returned["anchor"]["target"]
    assert counts(db_session) == (2, 2, 3)
    db_session.refresh(t.record)
    t.record.collaboration_generation = uuid.uuid4()
    db_session.commit()
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)


def test_maximum_dependency_exact_retry_survives_creation_disable(client, groups, monkeypatch):
    t = groups
    payload = request(client, t)
    payload["dependency"] = base64.b64encode(bytes(65536)).decode()
    captured = capture(client, t, payload)
    original = thread(t, captured)
    accepted = send(client, t.w, original)
    monkeypatch.setattr(get_settings(), "collaboration_bases_groups_enabled", False)
    assert capture(client, t, payload) == captured and t.source.calls == 1
    assert send(client, t.w, original) == accepted
    get(client, t, f"/threads/{accepted['thread_id']}")
    payload["operation_id"] = str(uuid.uuid4())
    assert "base_groups_disabled" in str(capture(client, t, payload, expected=409))
    send(client, t.w, thread(t, captured), expected=409)


def test_bounded_large_group_anchor_remains_usable_and_recoverable_after_disable(
    client, groups, monkeypatch
):
    t = groups
    unresolved = dict(
        type="unresolved_link",
        record_id=t.record.id,
        record_generation=t.captured["record_generation"],
        link="x",
        source_path_sha256=hashlib.sha256(t.record.path.encode()).hexdigest(),
    )
    value = {
        "type": "list",
        "value": [{"type": "text", "value": "\x01" * 4800}] + [unresolved] * 200,
    }
    t.source.source = "---\nstatus: " + json.dumps(["\x01" * 4800] + ["[[x]]"] * 200) + "\n---\n"
    assert len(t.source.source.encode()) < 65536
    captured = capture(client, t, request(client, t, value=value))
    original = thread(t, captured)
    assert len(json.dumps(original).encode()) > 65536
    accepted = send(client, t.w, original)
    monkeypatch.setattr(get_settings(), "collaboration_bases_groups_enabled", False)
    assert send(client, t.w, original) == accepted
    get(client, t, f"/threads/{accepted['thread_id']}")
    # Ordinary note commands retain their smaller limit.
    ordinary = create(t.w, "x" * 66000)
    send(client, t.w, ordinary, expected=413)


def test_retries_bind_payload_and_never_echo_private_source(client, groups):
    t = groups
    payload = request(client, t)
    t.source.source += "PRIVATE SOURCE BODY THAT MUST NOT LEAVE THE SERVER"
    captured = capture(client, t, payload)
    assert "PRIVATE SOURCE" not in str(captured)
    payload["value"] = {"type": "text", "value": "Other"}
    assert "operation_conflict" in str(capture(client, t, payload, expected=409))
    assert t.source.calls == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"group_id": str(uuid.uuid4())},
        {"witness_id": str(uuid.uuid4())},
        {"value": {"type": "text", "value": "Private guessed label"}},
    ],
)
def test_thread_cannot_forge_group_value_or_witness(client, groups, patch):
    t = groups
    captured = capture(client, t, request(client, t))
    forged = copy.deepcopy(captured)
    forged["anchor"]["target"].update(patch)
    send(client, t.w, thread(t, forged), expected=404)


def test_group_limit_rejects_new_values_but_keeps_existing_group_capture(
    client, db_session, groups, monkeypatch
):
    t = groups
    first = capture(client, t, request(client, t))
    monkeypatch.setattr(base_groups, "MAX_GROUPS", 1)
    assert capture(client, t, request(client, t))["anchor"] == first["anchor"]
    t.source.source = "---\nstatus: New\n---\n"
    assert "base_group_limit" in str(
        capture(client, t, request(client, t, value={"type": "text", "value": "New"}), expected=409)
    )
    assert counts(db_session) == (1, 1, 2)


def test_link_groups_require_every_reference_for_capture_mentions_and_later_access(
    client, db_session, groups
):
    t = groups
    linked = add_record(db_session, t)
    t.source.source = '---\nstatus: "[[Visible#Heading]]"\n---\n'
    value = dict(
        type="link",
        record_id=linked.id,
        record_generation=str(linked.collaboration_generation),
        subpath="#Heading",
    )
    payload = request(client, t, value=value)
    captured = capture(client, t, payload)
    original = thread(t, captured)
    accepted = send(client, t.w, original)
    witness = db_session.get(
        m.BaseGroupWitness, uuid.UUID(captured["anchor"]["target"]["witness_id"])
    )
    assert {ref["record_id"] for ref in witness.evidence["references"]} == {t.record.id, linked.id}
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reader.id,
            path=linked.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    mentioned = thread(t, captured)
    mentioned["command"]["body"]["blocks"][0]["children"].append(
        {"type": "mention", "user_id": str(t.w.reader.id)}
    )
    send(client, t.w, mentioned, expected=422)
    get(client, t, f"/threads/{accepted['thread_id']}", actor="reader", expected=404)
    get(client, t, f"/threads/{accepted['thread_id']}")
    db_session.add(
        m.ManagedRule(
            share_id=t.w.share.id,
            user_id=t.w.reviewer.id,
            path=linked.path,
            recursive=False,
            permission="none",
        )
    )
    db_session.commit()
    get(client, t, f"/threads/{accepted['thread_id']}", expected=404)
    send(client, t.w, original, expected=404)
    capture(client, t, payload, expected=404)
    payload["operation_id"] = str(uuid.uuid4())
    capture(client, t, payload, expected=404)


def test_ambiguous_link_or_uninitialized_link_lineage_cannot_mint_a_group(
    client, db_session, groups
):
    t = groups
    linked = add_record(db_session, t)
    value = dict(
        type="link",
        record_id=linked.id,
        record_generation=str(linked.collaboration_generation),
        subpath="",
    )
    t.source.source = '---\nstatus: "[[Visible]]"\n---\n'
    payload = request(client, t, value=value)
    duplicate = add_record(db_session, t, "Notes/Elsewhere/Visible.md")
    assert "base_group_value_unavailable" in str(capture(client, t, payload, expected=409))
    db_session.delete(duplicate)
    linked.collaboration_generation = None
    db_session.commit()
    assert "base_group_value_unavailable" in str(capture(client, t, payload, expected=409))
    assert counts(db_session) == (0, 0, 0)


def test_group_picture_reads_reauthorize_after_object_io(client, db_session, groups, pictures):
    t = groups
    captured = capture(client, t, request(client, t))
    picture_id, _ = upload(client, t.w, raster())
    original = thread(t, captured)
    original["command"]["body"] = picture_body(picture_id)
    send(client, t.w, original)
    get_picture(client, t.w, picture_id, actor="reviewer")
    pictures.before_get = lambda: deny_record(db_session, t)
    get_picture(client, t.w, picture_id, actor="reviewer", suffix="/thumbnail", expected=404)
    get_picture(client, t.w, picture_id, actor="reviewer", expected=404)


def test_definition_changes_preserve_old_threads_but_prevent_stale_new_capture(client, groups):
    t = groups
    payload = request(client, t)
    captured = capture(client, t, payload)
    original = thread(t, captured)
    accepted = send(client, t.w, original)
    # Presentation changes preserve a group, including its witness.
    configure(client, t, groupBy={"property": "note.status", "direction": "DESC"})
    # Rename separately: simultaneous name and definition changes intentionally
    # require identity repair in the saved-view registry.
    configure(client, t, title="Renamed", groupBy={"property": "note.status", "direction": "DESC"})
    current = capture(client, t, request(client, t))
    assert current["anchor"]["target"] == captured["anchor"]["target"]
    # Removal and restoration retain the definition ID but advance its semantic
    # revision: an old value cannot silently become a new group's identity.
    configure(client, t, title="Renamed")
    configure(client, t, title="Renamed", groupBy={"property": "status"})
    assert capture(client, t, payload) == captured
    payload["operation_id"] = str(uuid.uuid4())
    assert "base_target_definition_changed" in str(capture(client, t, payload, expected=409))
    fresh = capture(client, t, request(client, t))
    assert fresh["anchor"]["target"]["group_id"] != captured["anchor"]["target"]["group_id"]
    send(client, t.w, thread(t, captured), expected=409)
    assert send(client, t.w, original) == accepted
    get(client, t, f"/threads/{accepted['thread_id']}")
    send(
        client,
        t.w,
        envelope(
            t.w,
            dict(
                type="reply.create",
                thread_id=accepted["thread_id"],
                message_id=str(uuid.uuid4()),
                body=body("Keep discussing the historical group"),
            ),
        ),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"dependency": ""},
        {"dependency": "A==="},
        {"dependency": "AA"},
        {"dependency": base64.b64encode(bytes(65537)).decode()},
        {"value": {"type": "number", "value": True}},
        {"value": {"type": "number", "value": 10**310}},
        {"value": {"type": "text", "value": "private-input" * 8192}},
        {"value": {"type": "text", "value": "private-input", "source": "untrusted"}},
        {"value": {"type": "list", "value": [{"type": "empty"}] * 256}},
        {"value": {"type": "duration", "value": [1, 2]}},
        {"value": {"type": "file", "record_id": "private-input", "record_generation": "bad"}},
        {"time_zone": ""},
        {"registry_revision": True},
        {"source": "private-input"},
    ],
)
def test_invalid_capture_is_bounded_before_source_io_and_does_not_echo_inputs(
    client, db_session, groups, patch
):
    t = groups
    payload = request(client, t)
    payload.update(patch)
    response = capture(client, t, payload, expected=422)
    assert "private-input" not in str(response)
    assert t.source.calls == 0 and counts(db_session) == (0, 0, 0)

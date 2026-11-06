import uuid
from types import SimpleNamespace

import pytest
from test_bases_registry import base as base_fixture
from test_bases_registry import inspect, view
from test_discussion_pictures import get_picture, picture_body, raster, upload
from test_discussion_pictures import storage as picture_storage_fixture
from test_discussions import base as discussion_path
from test_discussions import body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import managed_service

world = discussion_world
base = base_fixture
storage = picture_storage_fixture


def context(client, w, view_id, *, record=None, actor="reviewer", expected=200):
    response = client.post(
        discussion_path(w) + "/bases/target-context",
        headers=headers(w.tokens[actor]),
        json={"view_id": view_id, **({"record_id": record.id} if record is not None else {})},
    )
    assert response.status_code == expected, response.text
    return response.json()


@pytest.fixture
def targets(client, db_session, world, base, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_discussions_enabled", True)
    snapshot = inspect(client, world)
    record_id = str(uuid.uuid4())
    record = m.ManagedEntry(
        id=record_id,
        share_id=world.share.id,
        path="Notes/Record.md",
        record={"type": "markdown", "id": record_id, "version": 0},
        deleted=False,
    )
    db_session.add(record)
    db_session.commit()
    captured = context(client, world, snapshot["views"][0]["id"], record=record)
    return SimpleNamespace(w=world, record=record, snapshot=snapshot, captured=captured, base=base)


@pytest.fixture
def pictures(targets, request):
    # Registry initialization uses its own storage adapter first. Only then
    # replace object I/O with the picture fixture's private in-memory store.
    return request.getfixturevalue("storage")


def anchor(t, kind="cell", *, second=False):
    value = {
        k: t.captured[k] for k in ("definition_id", "view_id", "generation", "registry_revision")
    }
    if second:
        value["view_id"] = t.snapshot["views"][1]["id"]
    target = {"type": kind}
    if kind in {"cell", "item"}:
        target.update({k: t.captured[k] for k in ("record_id", "record_generation")})
    if kind in {"cell", "property"}:
        target["property"] = {"source": "note", "name": "status"}
    return {"version": 1, "type": "base", **value, "target": target}


def command(t, kind="cell", **kwargs):
    payload = create(t.w)
    payload["command"]["anchor"] = anchor(t, kind, **kwargs)
    return payload


def deny_record(db, t, actor="reviewer", *, read=True):
    cls = m.ManagedRule if read else m.ManagedCommentRule
    values = dict(permission="none") if read else dict(allowed=False)
    db.add(
        cls(
            share_id=t.w.share.id,
            user_id=getattr(t.w, actor).id,
            path=t.record.path,
            recursive=False,
            **values,
        )
    )
    db.commit()


def get(client, t, suffix, *, actor="reviewer", expected=200, **params):
    response = client.get(
        discussion_path(t.w) + suffix, headers=headers(t.w.tokens[actor]), params=params
    )
    assert response.status_code == expected, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["view", "item", "cell", "property"])
def test_reviewer_targets_have_separate_saved_view_lists_and_cursors(client, targets, kind):
    t = targets
    first = send(client, t.w, command(t, kind))
    second = send(client, t.w, command(t, kind, second=True))
    page = get(client, t, "/threads", view_id=t.captured["view_id"])
    assert [item["id"] for item in page["threads"]] == [first["thread_id"]]
    assert page["scanned_through"] == first["sequence"]
    other = get(client, t, "/threads", view_id=t.snapshot["views"][1]["id"])
    assert [item["id"] for item in other["threads"]] == [second["thread_id"]]
    assert get(client, t, f"/threads/{first['thread_id']}")["anchor"]["target"]["type"] == kind
    events = get(client, t, "/changes", view_id=t.captured["view_id"])
    assert [event["data"]["thread_id"] for event in events["events"]] == [first["thread_id"]]
    get(
        client,
        t,
        "/changes",
        view_id=t.snapshot["views"][1]["id"],
        cursor=events["cursor"],
        expected=409,
    )
    get(client, t, "/threads", expected=422)
    send(client, t.w, command(t, kind), actor="reader", expected=403)


def test_context_is_read_only_and_cannot_capture_an_inaccessible_or_foreign_record(
    client, db_session, targets
):
    t = targets
    assert (
        context(client, t.w, t.captured["view_id"], record=t.record, actor="reader") == t.captured
    )
    context(client, t.w, t.captured["view_id"], record=t.w.other, expected=404)
    context(client, t.w, str(uuid.uuid4()), record=t.record, expected=404)
    assert db_session.get(m.ManagedEntry, t.record.id).record == {
        "type": "markdown",
        "id": t.record.id,
        "version": 0,
    }
    assert (
        db_session.get(m.ManagedEntry, t.w.entry.id).record["hash"] == t.snapshot["content_sha256"]
    )
    caps = client.get(
        "/v1/collaboration/capabilities",
        params={"share_id": str(t.w.share.id)},
        headers=headers(t.w.tokens["reader"]),
    ).json()
    assert caps["features"]["discussion_bases"] is True
    assert caps["discussion_base_targets"] == ["view", "item", "cell", "property"]


def test_record_comment_revocation_blocks_every_mutation_and_accepted_retry_but_not_reading(
    client, db_session, targets
):
    t = targets
    original = command(t)
    result = send(client, t.w, original)
    deny_record(db_session, t, read=False)
    send(client, t.w, original, expected=403)
    send(client, t.w, command(t), expected=403)
    thread, message = result["thread_id"], result["message_id"]
    mutations = [
        {
            "type": "reply.create",
            "thread_id": thread,
            "message_id": str(uuid.uuid4()),
            "body": body(),
        },
        {
            "type": "message.edit",
            "thread_id": thread,
            "message_id": message,
            "expected_revision": 1,
            "body": body("Edited"),
        },
        {
            "type": "message.delete",
            "thread_id": thread,
            "message_id": message,
            "expected_revision": 1,
        },
        {
            "type": "reaction.set",
            "thread_id": thread,
            "message_id": message,
            "emoji": "heart",
            "present": True,
        },
        {
            "type": "thread.state",
            "thread_id": thread,
            "state": "resolved",
            "expected_state_revision": 1,
        },
    ]
    for mutation in mutations:
        send(client, t.w, envelope(t.w, mutation), expected=403)
    assert get(client, t, f"/threads/{thread}")["messages"][0]["revision"] == 1
    # A view-level thread has no record intersection.
    send(client, t.w, command(t, "view"))


@pytest.mark.parametrize("change", ["read", "delete", "generation", "type"])
def test_record_loss_hides_all_thread_reads_receipts_and_events(
    client, db_session, targets, change
):
    t = targets
    original = command(t)
    result = send(client, t.w, original)
    if change == "read":
        deny_record(db_session, t)
    else:
        record = db_session.get(m.ManagedEntry, t.record.id)
        if change == "delete":
            record.deleted = True
        elif change == "generation":
            record.collaboration_generation = uuid.uuid4()
        else:
            record.record = {"type": "file"}
        db_session.commit()
    thread, message = result["thread_id"], result["message_id"]
    for suffix in [
        f"/threads/{thread}",
        f"/threads/{thread}/state",
        f"/threads/{thread}/messages/{message}",
        f"/threads/{thread}/messages/{message}/history",
    ]:
        get(client, t, suffix, expected=404)
    send(client, t.w, original, expected=404)
    assert (
        client.post(
            "/v1/collaboration/commands/withdraw",
            headers=headers(t.w.tokens["reviewer"]),
            json=original,
        ).status_code
        == 404
    )
    assert get(client, t, "/threads", view_id=t.captured["view_id"])["threads"] == []
    assert get(client, t, "/changes", view_id=t.captured["view_id"])["events"] == []


def test_hidden_candidates_advance_bounded_pages_without_leaking_identifiers(client, targets):
    t = targets
    hidden_context = context(client, t.w, t.captured["view_id"], record=t.w.other, actor="owner")
    hidden = command(t)
    hidden["command"]["anchor"]["target"].update(
        {k: hidden_context[k] for k in ("record_id", "record_generation")}
    )
    secret = send(client, t.w, hidden, actor="owner")
    visible = send(client, t.w, command(t, "view"))
    page = get(client, t, "/threads", view_id=t.captured["view_id"], limit=1)
    assert page["threads"] == [] and page["has_more"]
    assert page["scanned_through"] == page["next_after"] == secret["sequence"]
    next_page = get(
        client, t, "/threads", view_id=t.captured["view_id"], limit=1, after=page["next_after"]
    )
    assert [item["id"] for item in next_page["threads"]] == [visible["thread_id"]]
    events = get(client, t, "/changes", view_id=t.captured["view_id"], limit=1)
    assert events["events"] == [] and events["has_more"] and events["filtered"]
    following = get(
        client, t, "/changes", view_id=t.captured["view_id"], cursor=events["cursor"], limit=1
    )
    assert [item["data"]["thread_id"] for item in following["events"]] == [visible["thread_id"]]
    assert secret["thread_id"] not in str(page) + str(events)


def test_mentions_require_access_to_the_target_record(client, db_session, targets):
    t = targets
    deny_record(db_session, t, actor="reader")
    payload = command(t)
    payload["command"]["body"]["blocks"][0]["children"].append(
        {"type": "mention", "user_id": str(t.w.reader.id)}
    )
    send(client, t.w, payload, expected=422)
    payload["command"]["anchor"] = anchor(t, "view")
    send(client, t.w, payload)


def test_old_registry_revision_survives_rename_but_new_threads_require_a_current_active_view(
    client, targets
):
    t = targets
    payload = command(t)
    t.base.save([view("Renamed"), view("Cards", "cards")])
    send(client, t.w, payload, expected=409)  # Definition must first be inspected.
    renamed = inspect(client, t.w)
    assert renamed["views"][0]["id"] == t.captured["view_id"]
    result = send(client, t.w, payload)
    t.base.save([view("Cards", "cards")])
    current = inspect(client, t.w)
    assert current["views"][0]["id"] == t.snapshot["views"][1]["id"]
    send(client, t.w, command(t), expected=409)
    assert (
        get(client, t, f"/threads/{result['thread_id']}")["anchor"]["view_id"]
        == t.captured["view_id"]
    )
    assert (
        get(client, t, "/threads", view_id=t.captured["view_id"])["threads"][0]["id"]
        == result["thread_id"]
    )
    send(
        client,
        t.w,
        envelope(
            t.w,
            {
                "type": "reply.create",
                "thread_id": result["thread_id"],
                "message_id": str(uuid.uuid4()),
                "body": body("Archive reply"),
            },
        ),
    )


def test_creation_disable_keeps_prior_reads_replies_and_exact_receipts(
    client, targets, monkeypatch
):
    t = targets
    payload = command(t)
    result = send(client, t.w, payload)
    monkeypatch.setattr(get_settings(), "collaboration_bases_discussions_enabled", False)
    assert send(client, t.w, payload) == result
    send(client, t.w, command(t), expected=409)
    get(client, t, f"/threads/{result['thread_id']}")
    send(
        client,
        t.w,
        envelope(
            t.w,
            {
                "type": "reply.create",
                "thread_id": result["thread_id"],
                "message_id": str(uuid.uuid4()),
                "body": body(),
            },
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "definition_id",
        "view_id",
        "generation",
        "registry_revision",
        "record_generation",
        "property",
        "scope",
        "extra",
    ],
)
def test_forged_or_unsupported_anchor_evidence_is_rejected(client, targets, field):
    t = targets
    payload = command(t)
    value = payload["command"]["anchor"]
    if field in {"definition_id", "view_id", "generation"}:
        value[field] = str(uuid.uuid4())
    elif field == "registry_revision":
        value[field] += 100
    elif field == "record_generation":
        value["target"][field] = str(uuid.uuid4())
    elif field == "property":
        value["target"]["property"]["source"] = "formula"
    elif field == "scope":
        value = payload["command"]["anchor"] = {"version": 1, "type": "note"}
    else:
        value["target"]["dependencies"] = []
    response = client.post(
        "/v1/collaboration/commands", headers=headers(t.w.tokens["reviewer"]), json=payload
    )
    assert response.status_code in {404, 409, 422}, response.text


def test_pictures_in_base_threads_recheck_record_access_including_after_object_io(
    client, db_session, targets, pictures
):
    t = targets
    picture_id, _ = upload(client, t.w, raster())
    payload = command(t)
    payload["command"]["body"] = picture_body(picture_id)
    send(client, t.w, payload)
    get_picture(client, t.w, picture_id, actor="reviewer")
    get_picture(client, t.w, picture_id, actor="reviewer", suffix="/thumbnail")
    pictures.before_get = lambda: deny_record(db_session, t)
    get_picture(client, t.w, picture_id, actor="reviewer", suffix="/thumbnail", expected=404)
    get_picture(client, t.w, picture_id, actor="reviewer", expected=404)
    # An independently authorized reference can still make the same picture readable.
    view_comment = command(t, "view")
    view_comment["command"]["body"] = picture_body(picture_id)
    send(client, t.w, view_comment)
    get_picture(client, t.w, picture_id, actor="reviewer")


def test_permission_revoke_and_regrant_invalidate_previously_filtered_cursors(client, targets):
    t = targets
    result = send(client, t.w, command(t))
    cursor = get(client, t, "/changes", view_id=t.captured["view_id"])["cursor"]
    url = f"/v1/managed/shares/{t.w.share.id}/rules"
    owner = headers(t.w.tokens["owner"])
    original = client.get(url, headers=owner).json()
    denied = original + [
        {
            "user_id": str(t.w.reviewer.id),
            "path": t.record.path,
            "recursive": False,
            "permission": "none",
        }
    ]
    assert client.put(url, headers=owner, json=denied).status_code == 200
    get(client, t, "/changes", view_id=t.captured["view_id"], cursor=cursor, expected=409)
    assert get(client, t, "/threads", view_id=t.captured["view_id"])["threads"] == []
    hidden = get(client, t, "/changes", view_id=t.captured["view_id"])
    assert hidden["events"] == []
    assert client.put(url, headers=owner, json=original).status_code == 200
    get(client, t, "/changes", view_id=t.captured["view_id"], cursor=hidden["cursor"], expected=409)
    visible = get(client, t, "/threads", view_id=t.captured["view_id"])
    assert [item["id"] for item in visible["threads"]] == [result["thread_id"]]


@pytest.mark.parametrize("change", ["rename", "delete", "type", "content", "add"])
def test_inventory_changes_reset_cursors_only_when_existing_target_visibility_can_change(
    client, db_session, targets, change
):
    t = targets
    send(client, t.w, command(t))
    cursor = get(client, t, "/changes", view_id=t.captured["view_id"])["cursor"]
    previous = t.record.record.copy()
    changes = []
    if change in {"rename", "delete"}:
        changes.append({"path": t.record.path, "previous": previous, "value": None})
        if change == "rename":
            changes.append({"path": "Private/Moved.md", "previous": None, "value": previous})
    elif change == "add":
        changes.append(
            {
                "path": "Notes/New.md",
                "previous": None,
                "value": {"id": str(uuid.uuid4()), "version": 0, "type": "markdown"},
            }
        )
    else:
        value = {**previous, **({"type": "file"} if change == "type" else {"hash": "a" * 64})}
        changes.append({"path": t.record.path, "previous": previous, "value": value})
    managed_service.apply_batch(
        db_session, t.w.share, t.w.owner, managed_service.Batch(changes=changes)
    )
    structural = change in {"rename", "delete", "type"}
    get(
        client,
        t,
        "/changes",
        view_id=t.captured["view_id"],
        cursor=cursor,
        expected=409 if structural else 200,
    )
    visible = get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]
    assert len(visible) == (0 if structural else 1)


def test_comment_rule_changes_reset_cursors_without_hiding_readable_threads(client, targets):
    t = targets
    send(client, t.w, command(t))
    cursor = get(client, t, "/changes", view_id=t.captured["view_id"])["cursor"]
    url = f"/v1/collaboration/shares/{t.w.share.id}/comment-rules"
    owner = headers(t.w.tokens["owner"])
    current = client.get(url, headers=owner).json()
    changed = {
        "expected_revision": current["revision"],
        "rules": current["rules"]
        + [
            {
                "user_id": str(t.w.reviewer.id),
                "path": t.record.path,
                "recursive": False,
                "allowed": False,
            }
        ],
    }
    response = client.put(url, headers=owner, json=changed)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == current["revision"] + 1
    get(client, t, "/changes", view_id=t.captured["view_id"], cursor=cursor, expected=409)
    assert len(get(client, t, "/threads", view_id=t.captured["view_id"])["threads"]) == 1
    send(client, t.w, command(t), expected=403)

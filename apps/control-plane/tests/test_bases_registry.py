import hashlib
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_discussions import headers
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.services import bases_definition as codec
from app.services import bases_registry as service

world = discussion_world


def view(name, layout="table", **extra):
    return {"name": name, "type": layout, **extra}


class StoredResponse(io.BytesIO):
    def release_conn(self):
        self.released = True


@pytest.fixture
def base(world, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    storage = SimpleNamespace(raw=b"", gets=0, request_db=None, before_get=None, response=None)

    def get_object(bucket, key):
        assert not storage.request_db.in_transaction(), "Storage read holds SQL transaction"
        assert key == f"web-assets/{world.share.id}/{world.entry.id}"
        storage.gets += 1
        if storage.before_get:
            callback, storage.before_get = storage.before_get, None
            callback()
        storage.response = StoredResponse(storage.raw)
        return storage.response

    storage.get_object = get_object
    monkeypatch.setattr(service.storage_client, "get_minio_client", lambda: storage)
    synchronize = service.synchronize

    def observe(db, *args):
        storage.request_db = db
        return synchronize(db, *args)

    monkeypatch.setattr(service, "synchronize", observe)

    def save(views, **extra):
        storage.raw = json.dumps({"views": views, **extra}).encode()
        world.entry.path = "Notes/Planning.base"
        world.entry.record = {"type": "file", "hash": hashlib.sha256(storage.raw).hexdigest()}
        db_session.commit()
        return world.entry.record["hash"]

    storage.save = save
    save([view("Table"), view("Cards", "cards")])
    return storage


def path(w):
    return f"/v1/collaboration/shares/{w.share.id}/entries/{w.entry.id}/bases"


def inspect(client, w, *, actor="reviewer", expected=200, sha=None):
    response = client.post(
        path(w) + "/inspect",
        headers=headers(w.tokens[actor]),
        json={"content_sha256": sha or w.entry.record["hash"]},
    )
    assert response.status_code == expected, response.text
    return response.json()


def repair(
    client, w, snapshot, bindings, *, actor="owner", expected=200, archive=None, operation=None
):
    payload = {
        "content_sha256": w.entry.record["hash"],
        "expected_revision": snapshot["revision"],
        "operation_id": operation or str(uuid.uuid4()),
        "bindings": bindings,
        "archive": archive or [],
    }
    response = client.post(path(w) + "/reconcile", headers=headers(w.tokens[actor]), json=payload)
    assert response.status_code == expected, response.text
    return response.json(), payload


def test_read_only_inspection_verifies_bytes_and_repeated_reads_do_not_reparse_or_write(
    client, db_session, world, base
):
    original = base.raw
    first = inspect(client, world)
    assert first["revision"] == 1 and all(v["id"] for v in first["views"])
    assert inspect(client, world, actor="reader") == first
    assert (
        base.gets == 1 and base.raw == original and base.response.closed and base.response.released
    )
    assert db_session.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == 1
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionScope)) == 0
    features = client.get(
        f"/v1/collaboration/capabilities?share_id={world.share.id}",
        headers=headers(world.tokens["reader"]),
    ).json()["features"]
    assert features["bases_registry"] is True and features["bases"] is False


def test_rename_reorder_layout_change_and_duplicate_have_distinct_identity_rules(
    client, world, base
):
    first = inspect(client, world)
    table_id, cards_id = [v["id"] for v in first["views"]]
    base.save([view("Cards", "cards"), view("Renamed table")])
    moved = inspect(client, world)
    assert [v["id"] for v in moved["views"]] == [cards_id, table_id]
    base.save([view("Cards", "cards"), view("Renamed table", "kanban"), view("Copy", "kanban")])
    copied = inspect(client, world)
    assert copied["views"][1]["id"] == table_id
    assert copied["views"][2]["id"] not in {table_id, cards_id, None}


def test_delete_then_same_named_recreation_does_not_restore_archived_identity(client, world, base):
    first = inspect(client, world)
    old_id = first["views"][0]["id"]
    base.save([view("Cards", "cards")])
    inspect(client, world)
    archived = client.get(
        path(world) + "/views?state=archived", headers=headers(world.tokens["reviewer"])
    ).json()
    assert archived["views"][0]["id"] == old_id
    base.save([view("Table"), view("Cards", "cards")])
    recreated = inspect(client, world)
    assert recreated["views"][0]["id"] != old_id
    # Explicit editor repair can restore a known archived association, without merging.
    repaired, _ = repair(
        client,
        world,
        recreated,
        [{"slot": 0, "view_id": old_id}, {"slot": 1, "view_id": recreated["views"][1]["id"]}],
        archive=[recreated["views"][0]["id"]],
    )
    assert repaired["views"][0]["id"] == old_id


def test_ambiguous_changes_stay_unmatched_until_authorized_cas_repair(client, world, base):
    first = inspect(client, world)
    old_ids = [v["id"] for v in first["views"]]
    base.save([view("Calendar", "sabidurian-timeline"), view("List", "list")])
    ambiguous = inspect(client, world)
    assert [v["id"] for v in ambiguous["views"]] == [None, None]
    assert {v["id"] for v in ambiguous["unmatched"]["views"]} == set(old_ids)
    assignments = [{"slot": 0, "view_id": old_ids[1]}, {"slot": 1, "view_id": old_ids[0]}]
    repair(client, world, ambiguous, assignments, actor="reviewer", expected=403)
    repaired, payload = repair(client, world, ambiguous, assignments)
    assert [v["id"] for v in repaired["views"]] == list(reversed(old_ids))
    assert repaired["unmatched"]["views"] == []
    assert (
        client.post(
            path(world) + "/reconcile", headers=headers(world.tokens["owner"]), json=payload
        ).json()
        == repaired
    )
    repair(client, world, ambiguous, assignments, expected=409)
    payload["bindings"] = list(reversed(assignments))
    # Array order is part of the exact intent; operation reuse with changed bytes fails.
    assert (
        client.post(
            path(world) + "/reconcile", headers=headers(world.tokens["owner"]), json=payload
        ).status_code
        == 409
    )


def test_pending_associations_are_not_guessed_after_another_external_edit(client, world, base):
    first = inspect(client, world)
    base.save([view("Changed", "list")])
    ambiguous = inspect(client, world)
    assert ambiguous["views"][0]["id"] is None
    base.save([view("Table")])
    still_unmatched = inspect(client, world)
    assert still_unmatched["views"][0]["id"] is None
    assert {v["id"] for v in still_unmatched["unmatched"]["views"]} == {
        v["id"] for v in first["views"]
    }


def test_repair_rejects_duplicate_missing_foreign_or_archived_and_bound_ids(client, world, base):
    first = inspect(client, world)
    first_id = first["views"][0]["id"]
    repair(client, world, first, [{"slot": 0, "view_id": first_id}], expected=422)
    repair(
        client,
        world,
        first,
        [{"slot": 0, "view_id": first_id}, {"slot": 1, "view_id": first_id}],
        expected=422,
    )
    repair(
        client,
        world,
        first,
        [{"slot": 0, "view_id": str(uuid.uuid4())}, {"slot": 1, "view_id": None}],
        expected=404,
    )
    repair(
        client,
        world,
        first,
        [{"slot": 0, "view_id": first_id}, {"slot": 1, "view_id": None}],
        archive=[first_id],
        expected=422,
    )
    assert inspect(client, world) == first


def test_content_hash_and_generation_must_match_before_any_identity_can_be_created(
    client, db_session, world, base
):
    inspect(client, world, sha="0" * 64, expected=409)
    assert base.gets == 0
    base.raw += b" "
    inspect(client, world, expected=409)
    assert db_session.scalar(select(func.count()).select_from(m.BaseDefinition)) == 0
    base.raw = base.raw[:-1]
    inspect(client, world)
    world.entry.collaboration_generation = uuid.uuid4()
    db_session.commit()
    inspect(client, world, expected=409)


@pytest.mark.parametrize("change", ["revoke", "delete", "hash", "generation", "kind"])
def test_revalidation_after_io_blocks_changed_or_inaccessible_definitions(
    client, db_session, world, base, change
):
    def mutate():
        if change == "revoke":
            rule = db_session.scalar(
                select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
            )
            rule.permission = "none"
        elif change == "delete":
            world.entry.deleted = True
        elif change == "hash":
            world.entry.record = {"type": "file", "hash": "0" * 64}
        elif change == "generation":
            world.entry.collaboration_generation = uuid.uuid4()
        else:
            world.entry.record = {**world.entry.record, "type": "markdown"}
        db_session.commit()

    base.before_get = mutate
    inspect(client, world, expected=404 if change in {"revoke", "delete", "kind"} else 409)
    assert db_session.scalar(select(func.count()).select_from(m.BaseDefinition)) == 0


def test_registry_reads_and_exact_repair_receipts_recheck_current_access(
    client, db_session, world, base
):
    first = inspect(client, world)
    world.entry.path = "Private/Planning.base"
    db_session.commit()
    inspect(client, world, expected=404)
    assert (
        client.get(
            path(world) + "/views?state=archived", headers=headers(world.tokens["reviewer"])
        ).status_code
        == 404
    )
    owner = inspect(client, world, actor="owner")
    assert owner["definition_id"] == first["definition_id"]


def test_disabled_preview_and_oversized_requests_are_bounded(client, world, base, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", False)
    inspect(client, world, expected=404)
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    assert (
        client.post(
            path(world) + "/inspect", headers=headers(world.tokens["owner"]), content=b"x" * 65537
        ).status_code
        == 413
    )
    base.raw = b"x" * (codec.MAX_BYTES + 1)
    inspect(client, world, expected=413)


def test_exact_repair_receipt_does_not_bypass_lost_edit_permission(client, db_session, world, base):
    first = inspect(client, world)
    rule = db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
    )
    rule.permission = "write"
    # Config editors need no extra comment grant to repair view identity.
    db_session.scalar(
        select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == world.reviewer.id)
    ).allowed = False
    db_session.commit()
    assignments = [{"slot": v["slot"], "view_id": v["id"]} for v in first["views"]]
    _, payload = repair(client, world, first, assignments, actor="reviewer")
    rule.permission = "read"
    db_session.commit()
    response = client.post(
        path(world) + "/reconcile", headers=headers(world.tokens["reviewer"]), json=payload
    )
    assert response.status_code == 403
    assert inspect(client, world)["revision"] == 2


def test_restore_fences_repairs_before_receipts_but_status_remains_read_only(
    client, db_session, world, base
):
    first = inspect(client, world)
    assert first["restore_guard"] is True and first["restore_generation"] is None
    bindings = [{"slot": v["slot"], "view_id": v["id"]} for v in first["views"]]
    accepted, payload = repair(client, world, first, bindings)
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()
    current = inspect(client, world)
    assert current["revision"] == accepted["revision"]
    assert current["restore_generation"] == str(generation)
    fresh = {**payload, "operation_id": str(uuid.uuid4()), "expected_revision": current["revision"]}
    for request in (payload, fresh):
        for extra in ({}, {"X-Relay-Restore-Generation": str(uuid.uuid4())}):
            rejected = client.post(
                path(world) + "/reconcile",
                json=request,
                headers={**headers(world.tokens["owner"]), **extra},
            )
            assert rejected.status_code == 409
            assert rejected.json()["error"]["message"]["code"] == "restore_generation_changed"
    status = client.post(
        path(world) + "/reconcile/status", json=payload, headers=headers(world.tokens["owner"])
    )
    assert status.json() == {"status": "accepted", "result": accepted}
    assert client.post(
        path(world) + "/reconcile/status", json=fresh, headers=headers(world.tokens["owner"])
    ).json() == {
        "status": "unknown",
        "operation_id": fresh["operation_id"],
        "entry_id": world.entry.id,
    }
    changed = {**payload, "archive": [str(uuid.uuid4())]}
    assert (
        client.post(
            path(world) + "/reconcile/status", json=changed, headers=headers(world.tokens["owner"])
        ).status_code
        == 409
    )
    # Actor-owned receipts are not disclosed to another authorized reader.
    assert (
        client.post(
            path(world) + "/reconcile/status",
            json=payload,
            headers=headers(world.tokens["reviewer"]),
        ).json()["status"]
        == "unknown"
    )
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == 2
    updated = client.post(
        path(world) + "/reconcile",
        json=fresh,
        headers={**headers(world.tokens["owner"]), "X-Relay-Restore-Generation": str(generation)},
    )
    assert updated.status_code == 200 and updated.json()["restore_generation"] == str(generation)


@pytest.mark.parametrize("manual", [False, True])
def test_restore_during_unlocked_definition_read_cannot_publish_registry(
    client, db_session, world, base, manual
):
    first = inspect(client, world) if manual else None
    if manual:
        base.save([view("Changed")])
    before = db_session.scalar(select(func.count()).select_from(m.BaseRegistryRevision))

    def restore():
        world.share.collaboration_restore_generation = uuid.uuid4()
        db_session.commit()

    base.before_get = restore
    if manual:
        result, _ = repair(client, world, first, [{"slot": 0, "view_id": None}], expected=409)
    else:
        result = inspect(client, world, expected=409)
    assert result["error"]["message"]["code"] == "restore_generation_changed"
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == before


def test_historical_repair_status_uses_current_read_access_without_source_or_edit_access(
    client, db_session, world, base
):
    first = inspect(client, world)
    rule = db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
    )
    rule.permission = "write"
    db_session.commit()
    accepted, payload = repair(
        client,
        world,
        first,
        [{"slot": v["slot"], "view_id": v["id"]} for v in first["views"]],
        actor="reviewer",
    )
    rule.permission = "read"
    world.share.collaboration_restore_generation = uuid.uuid4()
    world.entry.collaboration_generation = uuid.uuid4()
    db_session.commit()
    result = client.post(
        path(world) + "/reconcile/status", json=payload, headers=headers(world.tokens["reviewer"])
    )
    assert result.status_code == 200 and result.json() == {"status": "accepted", "result": accepted}
    rule.permission = "none"
    db_session.commit()
    assert (
        client.post(
            path(world) + "/reconcile/status",
            json=payload,
            headers=headers(world.tokens["reviewer"]),
        ).status_code
        == 404
    )


def test_archived_view_pages_are_bounded_and_resume_without_duplicates(
    client, db_session, world, base
):
    first = inspect(client, world)
    definition_id = uuid.UUID(first["definition_id"])
    ids = sorted([uuid.uuid4() for _ in range(103)])
    db_session.add_all(
        [
            m.BaseView(
                id=view_id,
                definition_id=definition_id,
                state="archived",
                descriptor=view("Archived"),
            )
            for view_id in ids
        ]
    )
    db_session.commit()
    page = client.get(
        path(world) + "/views?state=archived", headers=headers(world.tokens["reviewer"])
    ).json()
    assert len(page["views"]) == 100 and page["has_more"] is True
    assert [v["id"] for v in page["views"]] == list(map(str, ids[:100]))
    following = client.get(
        path(world) + "/views?state=archived&after=" + page["next_after"],
        headers=headers(world.tokens["reviewer"]),
    ).json()
    assert [v["id"] for v in following["views"]] == list(map(str, ids[100:]))
    assert following["has_more"] is False


def test_parser_preserves_names_dates_and_formula_strings_without_evaluation():
    parsed = codec.parse(
        b"views:\n  - name: On\n    type: table\n    hidden: false\n"
        b"    start: 2026-11-06\n    width: 120\nformulas:\n  label: 'file.name + \"!\"'\n"
    )
    assert parsed.views[0] == dict(
        name="On", type="table", hidden=False, start="2026-11-06", width=120
    )
    assert parsed.value["formulas"]["label"] == 'file.name + "!"'


@pytest.mark.parametrize(
    "raw",
    [
        b"views: []\nviews: []",
        b"views: &x [*x]",
        b"views: !!python/object:evil {}",
        b"views: !!set {}",
        b"views: [{name: One, type: table}, {name: One, type: cards}]",
        b"views: [{name: 2, type: table}]",
        b"views: [{name: One, type: table, x: !!float .nan}]",
        b"views: []\nx: " + b"[" * 40 + b"]" * 40,
        b"views: []\n---\nviews: []",
        b"views: []\nx: 99999999999999999999999999",
    ],
)
def test_invalid_yaml_cannot_create_unbounded_ambiguous_or_executable_definitions(raw):
    with pytest.raises(codec.InvalidBase):
        codec.parse(raw)


def test_identical_rename_candidates_are_never_matched_by_array_order():
    previous = [
        {"id": str(i), "state": "active", "descriptor": view(name)}
        for i, name in enumerate(["First", "Second"])
    ]
    matched, unmatched, archived = codec.reconcile(previous, [view("Left"), view("Right")])
    assert matched == {} and set(unmatched) == {"0", "1"} and archived == []


def test_disappearance_and_duplicate_cannot_be_mistaken_for_a_rename():
    previous = [
        {"id": str(i), "state": "active", "descriptor": view(name)}
        for i, name in enumerate(["First", "Second"])
    ]
    matched, unmatched, archived = codec.reconcile(previous, [view("Second"), view("Copy")])
    assert matched == {0: "1"} and unmatched == ["0"] and archived == []


def test_empty_yaml_values_are_null_instead_of_strings():
    assert codec.parse(b"views: []\nfilters:\n").value["filters"] is None

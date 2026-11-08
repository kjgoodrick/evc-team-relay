import base64
import copy
import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from test_discussions import headers
from test_discussions import world as discussion_world

from app.core.config import get_settings
from app.db import models as m
from app.schemas.base_content import MAX_BYTES, PublishBaseContent
from app.services import base_content as service
from app.services import managed_service

world = discussion_world
INITIAL = b"views:\n  - name: Table\n    type: table\n"
CHANGED = b"views:\n  - name: Renamed\n    type: table\n"


@pytest.fixture
def content(world, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", True)
    world.entry.path = "Notes/Planning.base"
    world.entry.record = {
        "id": world.entry.id,
        "version": 0,
        "type": "file",
        "mimetype": "application/octet-stream",
        "hash": hashlib.sha256(INITIAL).hexdigest(),
        "synctime": 1,
    }
    db_session.commit()
    storage = SimpleNamespace(raw=INITIAL, reads=0, before_read=None)

    class Response(io.BytesIO):
        def release_conn(self):
            pass

    def get_object(bucket, key):
        storage.reads += 1
        if storage.before_read:
            callback, storage.before_read = storage.before_read, None
            callback()
        return Response(storage.raw)

    monkeypatch.setattr(
        service.storage_client, "get_minio_client", lambda: SimpleNamespace(get_object=get_object)
    )
    return storage


def path(w):
    return f"/v1/managed/shares/{w.share.id}/entries/{w.entry.id}/base-content"


def read(client, w, actor="reader", expected=200):
    response = client.get(path(w), headers=headers(w.tokens[actor]))
    assert response.status_code == expected, response.text
    return response.json()


def payload(state, raw=CHANGED):
    return {
        "operation_id": str(uuid.uuid4()),
        "expected": copy.deepcopy(state),
        "content_base64": base64.b64encode(raw).decode(),
    }


def send(client, w, body, actor="owner", expected=200):
    response = client.put(path(w), headers=headers(w.tokens[actor]), json=body)
    assert response.status_code == expected, response.text
    return response.json()


def test_legacy_read_then_atomic_publication_and_receipt_replay(client, world, content, db_session):
    initial = read(client, world)
    assert base64.b64decode(initial["content_base64"]) == INITIAL
    assert initial["state"]["content_version"] is None
    body = payload(initial["state"])
    first = send(client, world, body)
    assert first["state"]["content_version"] and first["state"]["generation"]
    stored = db_session.get(m.ManagedBaseContent, world.entry.id)
    db_session.refresh(world.entry)
    assert stored.data == CHANGED and stored.sha256 == world.entry.record["hash"]
    current = read(client, world)
    assert base64.b64decode(current["content_base64"]) == CHANGED
    assert content.reads == 1  # The legacy mutable object is never read again.
    second = send(client, world, payload(current["state"], b"views: []\n"))
    assert send(client, world, body) == first
    assert read(client, world)["state"] == second["state"]
    assert db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt)) == 2
    body["content_base64"] = base64.b64encode(b"different").decode()
    assert (
        send(client, world, body, expected=409)["error"]["message"]["code"]
        == "operation_id_conflict"
    )


def test_stale_writes_and_same_byte_aba_conflict(client, world, content):
    before = read(client, world)["state"]
    first = send(client, world, payload(before))
    assert (
        send(client, world, payload(before), expected=409)["error"]["message"]["code"]
        == "base_content_conflict"
    )
    second = send(client, world, payload(first["state"], INITIAL))
    third = send(client, world, payload(second["state"], CHANGED))
    assert third["state"]["record"]["hash"] == first["state"]["record"]["hash"]
    assert third["state"]["content_version"] != first["state"]["content_version"]
    send(client, world, payload(first["state"]), expected=409)


def test_comment_permission_cannot_publish_and_receipts_recheck_current_editor(
    client, world, content, db_session
):
    current = read(client, world)["state"]
    send(client, world, payload(current), actor="reviewer", expected=403)
    rule = db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
    )
    rule.permission = "write"
    db_session.scalar(
        select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == world.reviewer.id)
    ).allowed = False
    db_session.commit()
    body = payload(current)
    send(client, world, body, actor="reviewer")
    rule.permission = "read"
    db_session.commit()
    send(client, world, body, actor="reviewer", expected=403)
    rule.permission = "none"
    db_session.commit()
    read(client, world, actor="reviewer", expected=404)


def test_claimed_empty_state_is_distinct_from_zero_byte_publication(
    client, world, content, db_session
):
    world.entry.record = {"id": world.entry.id, "version": 0, "type": "file"}
    db_session.commit()
    initial = read(client, world)
    assert initial["content_base64"] is None and content.reads == 0
    send(client, world, payload(initial["state"], b""))
    assert read(client, world)["content_base64"] == ""


def test_legacy_read_releases_transaction_and_rechecks_revocation(
    client, world, content, db_session, monkeypatch
):
    real = service.read_legacy
    request_db = None
    original_read = service.read

    def observe(db, *args):
        nonlocal request_db
        request_db = db
        return original_read(db, *args)

    monkeypatch.setattr(service, "read", observe)

    def during_read(*args):
        assert not request_db.in_transaction()
        db_session.scalar(
            select(m.ManagedRule).where(m.ManagedRule.user_id == world.reader.id)
        ).permission = "none"
        db_session.commit()
        return real(*args)

    monkeypatch.setattr(service, "read_legacy", during_read)
    read(client, world, expected=404)


def test_legacy_hash_mismatch_or_oversize_never_publishes(client, world, content, db_session):
    content.raw = b"concurrent old-client upload"
    assert (
        read(client, world, expected=409)["error"]["message"]["code"] == "base_content_not_synced"
    )
    content.raw = b"x" * (MAX_BYTES + 1)
    read(client, world, expected=413)
    assert db_session.get(m.ManagedBaseContent, world.entry.id) is None


def test_bounded_bytes_preserve_invalid_yaml_and_validation_never_echoes_content(
    client, world, content
):
    before = read(client, world)["state"]
    raw = b"\xff" * MAX_BYTES  # Sync preserves bytes; parsing belongs to registry inspection.
    result = send(client, world, payload(before, raw))
    assert base64.b64decode(read(client, world)["content_base64"]) == raw
    send(client, world, payload(result["state"], raw + b"x"), expected=413)
    body = payload(result["state"])
    body["content_base64"] = "PRIVATE INVALID BODY!"
    response = send(client, world, body, expected=422)
    assert "PRIVATE" not in str(response)
    body["unknown"] = "PRIVATE UNKNOWN FIELD"
    assert "PRIVATE" not in str(send(client, world, body, expected=422))
    response = client.put(
        path(world), headers=headers(world.tokens["owner"]), content=b"x" * (MAX_BYTES * 2)
    )
    assert response.status_code == 413


def test_transaction_failure_rolls_back_content_inventory_and_receipt(
    client, world, content, db_session, monkeypatch
):
    before = read(client, world)["state"]

    def failure():
        db_session.flush()
        raise OperationalError("simulated commit failure", {}, Exception())

    monkeypatch.setattr(db_session, "commit", failure)
    with pytest.raises(HTTPException) as exc:
        service.publish(
            db_session,
            world.tokens["owner"],
            world.share.id,
            uuid.UUID(world.entry.id),
            PublishBaseContent.model_validate(payload(before)),
        )
    assert exc.value.status_code == 503
    assert db_session.get(m.ManagedBaseContent, world.entry.id) is None
    assert db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt)) == 0
    db_session.refresh(world.entry)
    assert world.entry.record == before["record"]


def test_registry_uses_guarded_bytes_instead_of_legacy_object(client, world, content, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_bases_registry_enabled", True)
    current = send(client, world, payload(read(client, world)["state"]))
    content.raw = b"obsolete legacy data"
    response = client.post(
        f"/v1/collaboration/shares/{world.share.id}/entries/{world.entry.id}/bases/inspect",
        headers=headers(world.tokens["reader"]),
        json={"content_sha256": current["state"]["record"]["hash"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["views"][0]["name"] == "Renamed"
    assert content.reads == 1


def file_token(client, world, sha):
    response = client.post(
        f"/shares/{world.share.id}/file-token",
        headers=headers(world.tokens["owner"]),
        json={
            "path": world.entry.id,
            "sha256": sha,
            "content_type": "application/yaml",
            "content_length": len(CHANGED),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_compatibility_reads_and_old_uploads_cannot_bypass_protection(
    client, world, content, monkeypatch
):
    before = read(client, world)["state"]
    old_token = file_token(client, world, before["record"]["hash"])
    route = f"/shares/{world.share.id}/files/{world.entry.id}"
    assert client.post(route + "/upload-url", headers=headers(old_token)).status_code == 428
    current = send(client, world, payload(before))
    token = file_token(client, world, current["state"]["record"]["hash"])
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", False)
    assert client.head(route, headers=headers(token)).status_code == 200
    response = client.get(route + "/download-url", headers=headers(token))
    assert response.status_code == 200, response.text
    assert client.get(response.json()["downloadUrl"]).content == CHANGED
    assert client.get(route + "/content", params={"token": old_token}).status_code == 409
    assert (
        client.put(route + "/content", params={"token": token}, content=b"overwrite").status_code
        == 428
    )
    assert content.reads == 1


def test_inventory_guard_preserves_moves_and_prevents_metadata_bypass_even_when_disabled(
    client, world, content, db_session, monkeypatch
):
    current = send(client, world, payload(read(client, world)["state"]))
    record = current["state"]["record"]
    db_session.commit()  # End the fixture session savepoint around HTTP commits.
    db_session.expire_all()
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", False)
    changed = {**record, "hash": "0" * 64}
    with pytest.raises(HTTPException) as exc:
        managed_service.apply_batch(
            db_session,
            world.share,
            world.owner,
            managed_service.Batch.model_validate(
                {"changes": [{"path": world.entry.path, "previous": record, "value": changed}]}
            ),
        )
    assert exc.value.status_code == 409
    db_session.rollback()
    manifest = managed_service.apply_batch(
        db_session,
        world.share,
        world.owner,
        managed_service.Batch.model_validate(
            {
                "changes": [
                    {"path": world.entry.path, "previous": record, "value": None},
                    {"path": "Notes/Renamed.txt", "previous": None, "value": record},
                ]
            }
        ),
    )
    assert manifest["records"]["Notes/Renamed.txt"] == record
    with pytest.raises(HTTPException):
        service.reject_legacy_write(db_session, world.share, world.entry.id)
    db_session.rollback()


def test_generation_change_never_reuses_guarded_content(client, world, content, db_session):
    body = payload(read(client, world)["state"])
    send(client, world, body)
    world.entry.collaboration_generation = uuid.uuid4()
    db_session.commit()
    assert (
        read(client, world, expected=409)["error"]["message"]["code"]
        == "base_content_lineage_changed"
    )
    send(client, world, body, expected=409)
    # Even a coherent restore into a new generation must not return old receipts.
    db_session.get(
        m.ManagedBaseContent, world.entry.id
    ).generation = world.entry.collaboration_generation
    db_session.commit()
    read(client, world)
    send(client, world, body, expected=409)


def test_manifest_reports_versions_only_for_currently_visible_entries(
    client, world, content, db_session
):
    current = send(client, world, payload(read(client, world)["state"]))
    db_session.commit()
    db_session.expire_all()
    manifest = managed_service.manifest(db_session, world.share, world.reader)
    assert manifest["base_content"] == {
        "protocol_version": 1,
        "enabled": True,
        "recovery": True,
        "versions": {world.entry.id: current["state"]["content_version"]},
        "retired": {},
    }
    db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reader.id)
    ).permission = "none"
    db_session.commit()
    assert (
        managed_service.manifest(db_session, world.share, world.reader)["base_content"]["versions"]
        == {}
    )


def test_deleted_guarded_identity_survives_rename_but_never_bypasses_current_acl(
    client, world, content, db_session, monkeypatch
):
    current = send(client, world, payload(read(client, world)["state"]))
    entry_id, record = world.entry.id, current["state"]["record"]
    db_session.commit()
    db_session.expire_all()

    def batch(changes):
        return managed_service.apply_batch(
            db_session,
            world.share,
            world.owner,
            managed_service.Batch.model_validate({"changes": changes}),
        )

    renamed = "Notes/Former-base.txt"
    batch(
        [
            {"path": world.entry.path, "previous": record, "value": None},
            {"path": renamed, "previous": None, "value": record},
        ]
    )
    removed = batch([{"path": renamed, "previous": record, "value": None}])
    assert removed["base_content"]["versions"] == {}
    assert removed["base_content"]["retired"] == {entry_id: renamed}
    assert removed["deleted"][renamed] == [entry_id]
    # Disable publication and create a different identity at the same path.
    # Tombstones must remain tied to the old identity, never its replacement.
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", False)
    replacement = {"id": str(uuid.uuid4()), "version": 0, "type": "file"}
    manifest = batch([{"path": renamed, "previous": None, "value": replacement}])
    assert manifest["records"][renamed] == replacement
    assert manifest["base_content"]["retired"] == {entry_id: renamed}
    assert not client.get(path(world), headers=headers(world.tokens["reader"])).is_success
    for actor in (world.reader, world.reviewer):
        assert managed_service.manifest(db_session, world.share, actor)["base_content"][
            "retired"
        ] == {entry_id: renamed}
    db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reader.id)
    ).permission = "none"
    db_session.commit()
    hidden = managed_service.manifest(db_session, world.share, world.reader)
    assert hidden["base_content"]["retired"] == {} and hidden["deleted"] == {}


def test_withdraw_fences_late_saves_and_checks_exact_intent_and_current_permission(
    client, world, content, monkeypatch
):
    initial = read(client, world)
    body = payload(initial["state"])
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", False)
    response = client.post(
        path(world) + "/withdraw", headers=headers(world.tokens["owner"]), json=body
    )
    assert response.status_code == 200 and response.json()["status"] == "withdrawn"
    assert (
        client.post(
            path(world) + "/withdraw", headers=headers(world.tokens["reviewer"]), json=body
        ).status_code
        == 403
    )
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", True)
    send(client, world, body, expected=410)
    assert read(client, world) == initial
    changed = {**body, "content_base64": base64.b64encode(b"replacement").decode()}
    send(client, world, changed, expected=409)
    accepted_body = payload(initial["state"])
    accepted = send(client, world, accepted_body)
    response = client.post(
        path(world) + "/withdraw", headers=headers(world.tokens["owner"]), json=accepted_body
    )
    assert response.json() == {"status": "accepted", "result": accepted}
    assert send(client, world, accepted_body) == accepted


def inspect_status(client, world, body, actor="owner", expected=200):
    response = client.post(path(world) + "/status", headers=headers(world.tokens[actor]), json=body)
    assert response.status_code == expected, response.text
    return response.json()


def test_read_only_receipts_survive_restore_and_edit_revocation_without_replaying(
    client, world, content, db_session, monkeypatch
):
    rule = db_session.scalar(
        select(m.ManagedRule).where(m.ManagedRule.user_id == world.reviewer.id)
    )
    rule.permission = "write"
    db_session.commit()
    body = payload(read(client, world)["state"])
    accepted = send(client, world, body, actor="reviewer")
    rule.permission = "read"
    world.share.collaboration_restore_generation = uuid.uuid4()
    world.entry.collaboration_generation = uuid.uuid4()
    db_session.commit()
    # Historical receipt inspection needs neither publish enablement nor a
    # coherent current source blob. It still requires current entry read access.
    monkeypatch.setattr(get_settings(), "managed_bases_sync_enabled", False)
    before = db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt))
    assert inspect_status(client, world, body, "reviewer") == {
        "status": "accepted",
        "result": accepted,
    }
    assert inspect_status(client, world, body, "reader")["status"] == "unknown"
    changed = {**body, "content_base64": base64.b64encode(b"different").decode()}
    assert (
        inspect_status(client, world, changed, "reviewer", 409)["error"]["message"]["code"]
        == "operation_id_conflict"
    )
    assert db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt)) == before
    assert db_session.get(m.ManagedBaseContent, world.entry.id).data == CHANGED
    rule.permission = "none"
    db_session.commit()
    inspect_status(client, world, body, "reviewer", 404)


def test_unknown_and_withdrawn_status_never_create_a_receipt_or_publish_bytes(
    client, world, content, db_session
):
    body = payload(read(client, world)["state"])

    def count():
        return db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt))

    assert count() == 0
    assert inspect_status(client, world, body) == {
        "status": "unknown",
        "operation_id": body["operation_id"],
        "entry_id": world.entry.id,
    }
    assert count() == 0 and db_session.get(m.ManagedBaseContent, world.entry.id) is None
    response = client.post(
        path(world) + "/withdraw", headers=headers(world.tokens["owner"]), json=body
    )
    assert response.status_code == 200
    assert inspect_status(client, world, body) == response.json()
    assert count() == 1 and db_session.get(m.ManagedBaseContent, world.entry.id) is None


def test_receipt_cannot_be_inspected_through_another_readable_base(
    client, world, content, db_session
):
    body = payload(read(client, world)["state"])
    send(client, world, body)
    other = m.ManagedEntry(
        id=str(uuid.uuid4()),
        share_id=world.share.id,
        path="Other.base",
        record={"type": "file"},
        deleted=False,
    )
    db_session.add(other)
    db_session.commit()
    response = client.post(
        path(world).replace(world.entry.id, other.id) + "/status",
        headers=headers(world.tokens["owner"]),
        json=body,
    )
    assert response.status_code == 404


def test_restore_guard_precedes_publication_and_withdrawal_receipt_replay(
    client, world, content, db_session
):
    before = read(client, world)
    assert before["restore_guard"] and before["restore_generation"] is None
    original = payload(before["state"])
    accepted = send(client, world, original)
    generation = uuid.uuid4()
    world.share.collaboration_restore_generation = generation
    db_session.commit()
    restored = read(client, world)
    assert restored["restore_generation"] == str(generation)
    pending = payload(restored["state"], INITIAL)
    for body in [original, pending]:
        for binding in [None, str(uuid.uuid4())]:
            auth = headers(world.tokens["owner"])
            if binding:
                auth["X-Relay-Restore-Generation"] = binding
            for method, url in [
                (client.put, path(world)),
                (client.post, path(world) + "/withdraw"),
            ]:
                response = method(url, headers=auth, json=body)
                assert response.status_code == 409, response.text
                assert response.json()["error"]["message"]["code"] == "restore_generation_changed"
            assert read(client, world) == restored
    assert inspect_status(client, world, original) == {"status": "accepted", "result": accepted}
    assert inspect_status(client, world, pending)["status"] == "unknown"
    auth = headers(world.tokens["owner"]) | {"X-Relay-Restore-Generation": str(generation)}
    response = client.put(path(world), headers=auth, json=pending)
    assert response.status_code == 200, response.text
    assert client.put(path(world), headers=auth, json=pending).json() == response.json()
    assert client.post(path(world) + "/withdraw", headers=auth, json=pending).json() == {
        "status": "accepted",
        "result": response.json(),
    }
    assert db_session.scalar(select(func.count()).select_from(m.ManagedBaseReceipt)) == 2

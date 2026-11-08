import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from minio.error import S3Error
from test_discussions import headers
from test_discussions import world as discussion_world

from app.core import security
from app.db import models as m
from app.services import managed_assets

world = discussion_world
DATA = b"synthetic image revision"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def assets(world, db_session, monkeypatch):
    world.entry.path = "Notes/photo.png"
    world.entry.record = {"id": world.entry.id, "type": "file", "hash": SHA}
    db_session.commit()
    state = SimpleNamespace(objects={}, writes=[], reads=[], after_io=None, responses=[])

    def after_io():
        if state.after_io:
            callback, state.after_io = state.after_io, None
            callback()

    class Response(io.BytesIO):
        headers = {"Content-Type": "image/png"}
        released = False

        def release_conn(self):
            self.released = True

    def missing():
        raise S3Error(None, "NoSuchKey", "fixture", "/fixture", "fixture", "fixture")

    def get_object(bucket, key):
        state.reads.append(key)
        if key not in state.objects:
            missing()
        response = Response(state.objects[key])
        state.responses.append(response)
        after_io()
        return response

    def stat_object(bucket, key):
        if key not in state.objects:
            missing()
        result = SimpleNamespace(size=len(state.objects[key]))
        after_io()
        return result

    def put_object(bucket, key, stream, length, content_type):
        data = stream.read()
        assert len(data) == length
        state.objects[key] = data
        state.writes.append(key)
        after_io()

    client = SimpleNamespace(
        get_object=get_object,
        stat_object=stat_object,
        put_object=put_object,
        bucket_exists=lambda bucket: True,
    )
    monkeypatch.setattr("app.api.routers.shares._get_minio_client", lambda: client)
    return state


def mint(
    client, w, *, binding=None, actor="owner", sha=SHA, length=len(DATA), expected=200,
    read_only=None,
):
    auth = headers(w.tokens[actor])
    if binding is not None:
        auth["X-Relay-Restore-Generation"] = str(binding)
    response = client.post(
        f"/shares/{w.share.id}/file-token",
        headers=auth,
        json={
            "path": w.entry.id,
            "sha256": sha,
            "content_type": "image/png",
            "content_length": length,
            **({"read_only": read_only} if read_only is not None else {}),
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


def content_url(w, grant):
    return f"/shares/{w.share.id}/files/{w.entry.id}/content?token={grant['token']}"


def upload_url(client, w, grant, expected=200):
    response = client.post(
        f"/shares/{w.share.id}/files/{w.entry.id}/upload-url", headers=headers(grant["token"])
    )
    assert response.status_code == expected, response.text
    return response


def rotate(db, w):
    db.refresh(w.share)
    w.share.collaboration_restore_generation = uuid.uuid4()
    db.commit()
    return w.share.collaboration_restore_generation


@pytest.mark.parametrize("actor", ["owner", "reader"])
@pytest.mark.parametrize("restored", [False, True])
def test_inspection_grant_reads_but_cannot_upload_even_for_owner(
    client, world, assets, db_session, actor, restored
):
    generation = rotate(db_session, world) if restored else None
    grant = mint(client, world, actor=actor, read_only=True, binding=generation)
    claims = security.decode_access_token(grant["token"])
    assert grant["read_only"] is claims["read_only"] is True
    assert claims["restore_guard"] is True
    assert claims["restore_generation"] == (str(generation) if generation else None)
    assets.objects[managed_assets.object_name(world.share.id, world.entry.id, SHA)] = DATA
    root = f"/shares/{world.share.id}/files/{world.entry.id}"
    auth = headers(grant["token"])
    assert client.head(root, headers=auth).status_code == 200
    assert client.get(root + "/download-url", headers=auth).status_code == 200
    assert client.get(content_url(world, grant)).content == DATA
    upload_url(client, world, grant, 403)
    assert client.put(content_url(world, grant), content=DATA).status_code == 403
    assert not assets.writes
    rotate(db_session, world)
    assert client.head(root, headers=auth).status_code == 409
    assert client.get(root + "/download-url", headers=auth).status_code == 409
    assert client.get(content_url(world, grant)).status_code == 409


@pytest.mark.parametrize("value", ["true", "false", 1, 0, {}, []])
def test_read_only_flag_is_strict(client, world, assets, value):
    mint(client, world, read_only=value, expected=422)


@pytest.mark.parametrize("change", ["restore", "revoke", "session"])
def test_inspection_rechecks_authority_after_object_read(
    client, world, assets, db_session, change
):
    grant = mint(client, world, actor="reader", read_only=True)
    assets.objects[managed_assets.object_name(world.share.id, world.entry.id, SHA)] = DATA

    def change_authority():
        if change == "restore":
            rotate(db_session, world)
        elif change == "revoke":
            rule = db_session.query(m.ManagedRule).filter_by(user_id=world.reader.id).one()
            rule.permission = "none"
            db_session.commit()
        else:
            session = db_session.get(m.UserSession, world.sessions["reader"])
            db_session.delete(session)
            db_session.commit()

    assets.after_io = change_authority
    assert client.get(content_url(world, grant)).status_code in {401, 403, 409}
    assert not assets.writes


def test_headerless_inspection_captures_current_generation_without_write_authority(
    client, world, assets, db_session
):
    generation = rotate(db_session, world)
    grant = mint(client, world, read_only=True)
    assert grant["restore_generation"] == str(generation)
    assert security.decode_access_token(grant["token"])["restore_generation"] == str(generation)
    upload_url(client, world, grant, 403)


def test_managed_upload_is_verified_and_old_credentials_cannot_cross_restore(
    client, world, assets, db_session
):
    original = mint(client, world)
    assert original["restore_guard"] is True and original["restore_generation"] is None
    assert "restore_generation" not in security.decode_access_token(original["token"])
    upload_url(client, world, original)
    result = client.put(content_url(world, original), content=DATA)
    assert result.status_code == 200, result.text
    key = managed_assets.object_name(world.share.id, world.entry.id, SHA)
    assert assets.objects == {key: DATA}
    restored = rotate(db_session, world)
    upload_url(client, world, original, 409)
    assert client.put(content_url(world, original), content=DATA).status_code == 409
    # A headerless client can obtain read access but never silently inherits a
    # new write binding from the response's current context.
    unbound = mint(client, world)
    assert unbound["restore_generation"] == str(restored)
    assert "restore_generation" not in security.decode_access_token(unbound["token"])
    upload_url(client, world, unbound, 409)
    bound = mint(client, world, binding=restored)
    assert security.decode_access_token(bound["token"])["restore_generation"] == str(restored)
    upload_url(client, world, bound)
    assert client.put(content_url(world, bound), content=DATA).status_code == 200
    mint(client, world, binding=uuid.uuid4(), expected=409)


@pytest.mark.parametrize(
    "body,length,sha",
    [
        (DATA + b"extra", len(DATA), SHA),
        (DATA[:-1], len(DATA), SHA),
        (b"x" * len(DATA), len(DATA), SHA),
    ],
)
def test_managed_upload_rejects_wrong_bytes_before_storage(
    client, world, assets, body, length, sha
):
    grant = mint(client, world, length=length, sha=sha)
    response = client.put(content_url(world, grant), content=body)
    assert response.status_code == 422, response.text
    assert not assets.writes


def test_managed_mint_validates_digest_length_and_readers_cannot_upload(client, world, assets):
    mint(client, world, sha="../bad", expected=422)
    mint(client, world, length=-1, expected=422)
    mint(client, world, length=26 * 1024 * 1024, expected=422)
    grant = mint(client, world, actor="reader")
    upload_url(client, world, grant, 403)
    assert client.put(content_url(world, grant), content=DATA).status_code == 403
    assert not assets.writes


def test_restore_during_body_prevents_object_write(client, world, assets, db_session):
    grant = mint(client, world)

    def body():
        rotate(db_session, world)
        yield DATA

    response = client.put(content_url(world, grant), content=body())
    assert response.status_code == 409, response.text
    assert not assets.writes


@pytest.mark.parametrize("change", ["restore", "revoke", "session"])
def test_change_during_object_upload_rejects_success_without_overwriting_restored_bytes(
    client, world, assets, db_session, change
):
    # Reader becomes a file editor for this upload; keep the owner available to
    # model a permission/restore change during the unlocked object write.
    rule = db_session.query(m.ManagedRule).filter_by(user_id=world.reader.id).one()
    rule.permission = "write"
    db_session.commit()
    grant = mint(client, world, actor="reader")
    restored_data = b"image selected by restored inventory"
    restored_key = managed_assets.object_name(
        world.share.id, world.entry.id, hashlib.sha256(restored_data).hexdigest()
    )
    assets.objects[restored_key] = restored_data

    def change_authority():
        if change == "restore":
            rotate(db_session, world)
        elif change == "revoke":
            rule.permission = "none"
            db_session.commit()
        else:
            session = db_session.get(m.UserSession, world.sessions["reader"])
            db_session.delete(session)
            db_session.commit()

    assets.after_io = change_authority
    response = client.put(content_url(world, grant), content=DATA)
    assert response.status_code in {401, 403, 409}, response.text
    assert assets.objects[restored_key] == restored_data
    assert assets.objects[managed_assets.object_name(world.share.id, world.entry.id, SHA)] == DATA


def test_managed_reads_verify_legacy_bytes_and_prefer_hash_keys(client, world, assets):
    grant = mint(client, world)
    legacy_key = f"web-assets/{world.share.id}/{world.entry.id}"
    assets.objects[legacy_key] = DATA
    root = f"/shares/{world.share.id}/files/{world.entry.id}"
    assert client.head(root, headers=headers(grant["token"])).status_code == 200
    assert client.get(content_url(world, grant)).content == DATA
    assets.objects[legacy_key] = b"different revision"
    assert client.head(root, headers=headers(grant["token"])).status_code == 409
    assert client.get(root + "/download-url", headers=headers(grant["token"])).status_code == 409
    assert client.get(content_url(world, grant)).status_code == 409
    assets.objects[managed_assets.object_name(world.share.id, world.entry.id, SHA)] = DATA
    assert client.head(root, headers=headers(grant["token"])).status_code == 200
    assert client.get(content_url(world, grant)).content == DATA
    assert all(response.closed and response.released for response in assets.responses)


@pytest.mark.parametrize("method", ["head", "get", "url"])
def test_restore_during_object_read_prevents_stale_delivery(
    client, world, assets, db_session, method
):
    grant = mint(client, world)
    assets.objects[managed_assets.object_name(world.share.id, world.entry.id, SHA)] = DATA
    assets.after_io = lambda: rotate(db_session, world)
    root = f"/shares/{world.share.id}/files/{world.entry.id}"
    if method == "head":
        response = client.head(root, headers=headers(grant["token"]))
    elif method == "url":
        response = client.get(root + "/download-url", headers=headers(grant["token"]))
    else:
        response = client.get(content_url(world, grant))
    assert response.status_code == 409, response.text

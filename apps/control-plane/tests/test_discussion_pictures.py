import asyncio
import hashlib
import importlib
import struct
import threading
import uuid
import zlib
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image, PngImagePlugin
from sqlalchemy import func, select
from test_discussions import base, body, create, envelope, headers, send
from test_discussions import world as discussion_world

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_picture_codec as codec
from app.services import discussion_pictures as pictures

world = discussion_world


def raster(fmt="PNG", size=(48, 32), **options):
    out = BytesIO()
    with Image.new("RGB", size, "#2057a2") as image:
        image.save(out, format=fmt, **options)
    return out.getvalue()


class StoredResponse(BytesIO):
    released = False

    def release_conn(self):
        self.released = True


class MemoryStorage:
    def __init__(self, db):
        self.db = db
        self.request_db = None
        self.objects = {}
        self.puts = 0
        self.gets = 0
        self.after_put = None
        self.before_get = None
        self.last_response = None

    def put_object(self, bucket, key, data, size, **kwargs):
        assert not self.request_db.in_transaction(), "Storage write holds database locks"
        assert key.startswith("discussion-pictures/")
        raw = data.read()
        assert len(raw) == size
        self.objects[key] = raw
        self.puts += 1
        if self.after_put:
            callback, self.after_put = self.after_put, None
            callback()

    def get_object(self, bucket, key):
        assert not self.request_db.in_transaction(), "Storage read holds database locks"
        self.gets += 1
        if self.before_get:
            callback, self.before_get = self.before_get, None
            callback()
        self.last_response = StoredResponse(self.objects[key])
        return self.last_response


@pytest.fixture
def storage(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "collaboration_pictures_enabled", True)
    store = MemoryStorage(db_session)
    monkeypatch.setattr(pictures.storage_client, "get_minio_client", lambda: store)

    # HTTP requests use their own Session; the fixture's inspection Session may
    # legitimately have an open transaction when reading world IDs.
    def observe(fn):
        def call(db, *args, **kwargs):
            store.request_db = db
            return fn(db, *args, **kwargs)

        return call

    monkeypatch.setattr(pictures, "upload", observe(pictures.upload))
    monkeypatch.setattr(pictures, "content", observe(pictures.content))
    return store


def stage(client, w, data, *, actor="reviewer", picture_id=None, expected=200, entry=None):
    payload = dict(
        picture_id=str(picture_id or uuid.uuid4()),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    response = client.post(
        base(w, entry) + "/pictures", json=payload, headers=headers(w.tokens[actor])
    )
    assert response.status_code == expected, response.text
    return payload, response.json()


def upload(client, w, data, *, actor="reviewer", picture_id=None, expected=200):
    if picture_id is None:
        payload, _ = stage(client, w, data, actor=actor)
        picture_id = payload["picture_id"]
    response = client.put(
        base(w) + f"/pictures/{picture_id}/content",
        content=data,
        headers={**headers(w.tokens[actor]), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == expected, response.text
    return picture_id, response.json()


def picture_body(picture_id):
    return {
        "version": 1,
        "blocks": [{"type": "picture", "picture_id": picture_id, "alt": "Example"}],
    }


def publish(client, w, picture_id):
    command = create(w)
    command["command"]["body"] = picture_body(picture_id)
    return command, send(client, w, command)


def get_picture(client, w, picture_id, actor="reader", suffix="", expected=200):
    response = client.get(
        base(w) + f"/pictures/{picture_id}" + suffix, headers=headers(w.tokens[actor])
    )
    assert response.status_code == expected, response.text
    return response


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_verified_pixels_and_thumbnail_have_measured_type_and_size(fmt):
    result = codec.verify(raster(fmt, size=(1024, 768)))
    assert result.source_mime == codec.FORMATS[fmt]
    assert (result.content.width, result.content.height) == (1024, 768)
    assert (result.thumbnail.width, result.thumbnail.height) == (512, 384)
    for rendition in (result.content, result.thumbnail):
        with Image.open(BytesIO(rendition.data)) as image:
            image.load()
            assert image.size == (rendition.width, rendition.height)
            assert rendition.mime in {"image/png", "image/jpeg"}
            assert not image.getexif()


def test_normalization_removes_metadata_and_applies_orientation():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "secret location"
    data = raster("JPEG", size=(80, 40), exif=exif)
    result = codec.verify(data)
    assert (result.content.width, result.content.height) == (40, 80)
    assert b"secret location" not in result.content.data
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "secret author")
    result = codec.verify(raster(pnginfo=info))
    assert b"secret author" not in result.content.data
    assert b"secret author" not in result.thumbnail.data


def test_alpha_survives_sanitizing_and_thumbnail():
    data = BytesIO()
    with Image.new("RGBA", (20, 20), (12, 34, 56, 78)) as image:
        image.save(data, format="PNG")
    result = codec.verify(data.getvalue())
    for rendition in (result.content, result.thumbnail):
        with Image.open(BytesIO(rendition.data)) as image:
            assert image.getpixel((0, 0)) == (12, 34, 56, 78)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"<html>hi</html>",
        raster()[:42],
        raster("BMP"),
    ],
)
def test_invalid_or_active_files_are_rejected(data):
    with pytest.raises(HTTPException) as error:
        codec.verify(data)
    assert error.value.status_code == 415


def test_animation_is_explicitly_rejected_without_silently_flattening():
    data = BytesIO()
    with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as second:
        first.save(data, format="GIF", save_all=True, append_images=[second])
    with pytest.raises(HTTPException) as error:
        codec.verify(data.getvalue())
    assert error.value.detail == {"code": "animated_picture_unsupported"}


def test_jpeg_factory_cannot_admit_an_unlisted_mpo_container():
    data = BytesIO()
    with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as second:
        first.save(data, format="MPO", save_all=True, append_images=[second])
    with pytest.raises(HTTPException) as error:
        codec.verify(data.getvalue())
    assert error.value.status_code == 415


def test_encoder_rejects_oversized_output_before_retaining_it():
    with Image.new("RGB", (30, 30), "red") as image:
        with pytest.raises(HTTPException) as error:
            codec.encode(image, limit=20)
    assert error.value.detail == {"code": "picture_output_too_large"}


def test_header_pixel_limit_is_checked_before_decode_and_bytes_are_bounded():
    raw = bytearray(raster())
    # Valid PNG IHDR/CRC with an excessive canvas, without allocating that canvas.
    raw[16:24] = struct.pack(">II", 5000, 4001)
    raw[29:33] = struct.pack(">I", zlib.crc32(raw[12:29]))
    with pytest.raises(HTTPException) as error:
        codec.verify(bytes(raw))
    assert error.value.detail == {"code": "picture_too_many_pixels"}
    with pytest.raises(HTTPException) as error:
        codec.verify(b"0" * (codec.MAX_BYTES + 1))
    assert error.value.status_code == 413


def test_reviewer_upload_is_private_until_message_transaction_commits(
    client, db_session, world, storage
):
    w = world
    data = raster()
    pid, verified = upload(client, w, data)
    assert verified["state"] == "verified"
    assert "key" not in str(verified["renditions"])
    assert storage.puts == 2
    for actor in ("reader", "owner"):
        get_picture(client, w, pid, actor=actor, expected=404)
        get_picture(client, w, pid, actor=actor, suffix="/content", expected=404)
    get_picture(client, w, pid, actor="reviewer", suffix="/content")
    command, result = publish(client, w, pid)
    assert get_picture(client, w, pid).json()["state"] == "attached"
    response = get_picture(client, w, pid, suffix="/thumbnail")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "image/png"
    assert storage.last_response.closed and storage.last_response.released
    assert send(client, w, command) == result
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionPictureReference)) == 1
    assert w.entry.record == {"type": "markdown"}


def test_staging_and_upload_retries_reuse_identity_and_objects(client, world, storage):
    w = world
    data = raster()
    payload, first = stage(client, w, data)
    _, second = stage(client, w, data, picture_id=payload["picture_id"])
    assert first == second
    pid, verified = upload(client, w, data, picture_id=payload["picture_id"])
    _, retried = upload(client, w, data, picture_id=pid)
    assert verified == retried
    assert storage.puts == 2
    stage(client, w, raster("JPEG"), picture_id=pid, expected=409)
    wrong = bytes([data[0] ^ 1]) + data[1:]
    upload(client, w, wrong, picture_id=pid, expected=422)
    assert storage.puts == 2


def test_reader_cannot_stage_and_foreign_user_or_scope_cannot_attach(
    client, db_session, world, storage
):
    w = world
    data = raster()
    stage(client, w, data, actor="reader", expected=403)
    pid, _ = upload(client, w, data)
    command = create(w)
    command["command"]["body"] = picture_body(pid)
    send(client, w, command, actor="owner", expected=404)
    # Owner can read both entries but cannot transplant their own private picture.
    owned, _ = upload(client, w, data, actor="owner")
    command["command"]["body"] = picture_body(owned)
    command["entry_id"] = w.other.id
    send(client, w, command, actor="owner", expected=404)
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionThread)) == 0
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionPictureReference)) == 0


def test_unverified_picture_cannot_publish_or_leave_partial_thread(
    client, db_session, world, storage
):
    w = world
    payload, _ = stage(client, w, raster())
    command = create(w)
    command["command"]["body"] = picture_body(payload["picture_id"])
    send(client, w, command, expected=409)
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionThread)) == 0
    assert db_session.scalar(select(func.count()).select_from(m.DiscussionReceipt)) == 0


def revoke_comment(db, w):
    rule = db.scalar(
        select(m.ManagedCommentRule).where(m.ManagedCommentRule.user_id == w.reviewer.id)
    )
    rule.allowed = False
    db.commit()


def test_revocation_after_staging_denies_upload_before_object_io(
    client, db_session, world, storage
):
    w = world
    data = raster()
    payload, _ = stage(client, w, data)
    revoke_comment(db_session, w)
    upload(client, w, data, picture_id=payload["picture_id"], expected=403)
    assert storage.puts == 0


def test_revocation_during_object_upload_prevents_finalize_and_publish(
    client, db_session, world, storage
):
    w = world
    data = raster()
    payload, _ = stage(client, w, data)
    storage.after_put = lambda: revoke_comment(db_session, w)
    upload(client, w, data, picture_id=payload["picture_id"], expected=403)
    stored = db_session.get(m.DiscussionPicture, uuid.UUID(payload["picture_id"]))
    assert stored.state == "staged" and stored.renditions is None
    get_picture(client, w, payload["picture_id"], expected=404)


def test_revocation_during_download_prevents_content_delivery(client, db_session, world, storage):
    w = world
    pid, _ = upload(client, w, raster())
    publish(client, w, pid)

    def revoke_read():
        rule = db_session.scalar(select(m.ManagedRule).where(m.ManagedRule.user_id == w.reader.id))
        rule.permission = "deny"
        db_session.commit()

    storage.before_get = revoke_read
    get_picture(client, w, pid, suffix="/content", expected=404)
    assert storage.last_response.closed and storage.last_response.released


def test_history_keeps_pictures_but_deletion_and_moderation_gate_access(client, world, storage):
    w = world
    pid, _ = upload(client, w, raster())
    _, accepted = publish(client, w, pid)
    tid, mid = accepted["thread_id"], accepted["message_id"]
    send(
        client,
        w,
        envelope(
            w,
            dict(
                type="message.edit",
                thread_id=tid,
                message_id=mid,
                expected_revision=1,
                body=body("Removed from current revision"),
            ),
        ),
    )
    get_picture(client, w, pid, suffix="/content")  # Still readable in history.
    send(
        client,
        w,
        envelope(
            w, dict(type="message.delete", thread_id=tid, message_id=mid, expected_revision=2)
        ),
    )
    get_picture(client, w, pid, suffix="/content", expected=404)
    get_picture(client, w, pid, actor="reviewer", suffix="/content")
    get_picture(client, w, pid, actor="owner", suffix="/content")
    send(
        client,
        w,
        envelope(
            w,
            dict(type="thread.remove", thread_id=tid, expected_state_revision=1, reason="Removed"),
        ),
        actor="owner",
    )
    get_picture(client, w, pid, actor="reviewer", suffix="/content", expected=404)
    get_picture(client, w, pid, actor="owner", suffix="/content")


def test_creation_switch_preserves_reads_receipts_and_retained_picture_edits(
    client, world, storage, monkeypatch
):
    w = world
    pid, _ = upload(client, w, raster())
    command, accepted = publish(client, w, pid)
    unused, _ = upload(client, w, raster("JPEG"))
    monkeypatch.setattr(get_settings(), "collaboration_pictures_enabled", False)
    get_picture(client, w, pid, suffix="/content")
    assert send(client, w, command) == accepted
    stage(client, w, raster(), expected=409)
    edit = envelope(
        w,
        dict(
            type="message.edit",
            thread_id=accepted["thread_id"],
            message_id=accepted["message_id"],
            expected_revision=1,
            body={
                "version": 1,
                "blocks": [*picture_body(pid)["blocks"], *body("Edited caption")["blocks"]],
            },
        ),
    )
    send(client, w, edit)
    new = create(w)
    new["command"]["body"] = picture_body(unused)
    send(client, w, new, expected=409)


def test_expiry_quota_capacity_and_stream_limits_are_recoverable(
    client, db_session, world, storage, monkeypatch
):
    w = world
    data = raster()
    payload, _ = stage(client, w, data)
    monkeypatch.setattr(pictures, "STAGING_COUNT", 1)
    stage(client, w, data, expected=409)
    assert pictures.upload_slots.acquire(blocking=False)
    try:
        _, error = upload(client, w, data, picture_id=payload["picture_id"], expected=503)
        assert "picture_capacity" in str(error)
    finally:
        pictures.upload_slots.release()
    upload(client, w, data + b"extra", picture_id=payload["picture_id"], expected=413)
    assert storage.puts == 0
    stored = db_session.get(m.DiscussionPicture, uuid.UUID(payload["picture_id"]))
    stored.expires_at = security.utcnow() - timedelta(seconds=1)
    db_session.commit()
    upload(client, w, data, picture_id=payload["picture_id"], expected=410)


def test_content_corruption_is_not_served(client, world, storage):
    pid, _ = upload(client, world, raster())
    publish(client, world, pid)
    for key in storage.objects:
        storage.objects[key] = b"corrupted"
    get_picture(client, world, pid, suffix="/content", expected=503)
    assert storage.last_response.closed and storage.last_response.released


def test_body_limits_and_no_untrusted_image_urls(client, world, storage):
    command = create(world)
    command["command"]["body"] = {
        "version": 1,
        "blocks": [
            dict(
                type="picture", picture_id=str(uuid.uuid4()), url="https://example.com/private.png"
            )
        ],
    }
    send(client, world, command, expected=422)
    command["command"]["body"] = {
        "version": 1,
        "blocks": [dict(type="picture", picture_id=str(uuid.uuid4())) for _ in range(11)],
    }
    send(client, world, command, expected=422)
    capabilities = client.get(
        f"/v1/collaboration/capabilities?share_id={world.share.id}",
        headers=headers(world.tokens["reviewer"]),
    ).json()
    assert capabilities["features"]["discussion_pictures"] is True
    assert capabilities["discussion_limits"]["picture_bytes"] == codec.MAX_BYTES


def test_worker_cancellation_keeps_decoder_admission_until_worker_exits(monkeypatch):
    from app.api.routers import discussion_pictures as api

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(pictures, "prepare_upload", lambda *args: SimpleNamespace(size=1))

    def slow_upload(*args):
        entered.set()
        assert release.wait(5)
        return {"state": "verified"}

    monkeypatch.setattr(pictures, "upload", slow_upload)

    class Request:
        async def stream(self):
            yield b"x"

    async def exercise():
        task = asyncio.create_task(
            api.upload(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), Request(), "token", None)
        )
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done()
                assert not pictures.upload_slots.acquire(blocking=False)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pictures.upload_slots.acquire(blocking=False)
        pictures.upload_slots.release()

    asyncio.run(exercise())


def test_picture_migration_upgrade_downgrade_and_composite_history_reference():
    migration = importlib.import_module(
        "app.db.migrations.versions.202611060001_discussion_pictures"
    )
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
                inspector = sa.inspect(connection)
                assert set(inspector.get_table_names()) == {
                    "discussion_pictures",
                    "discussion_picture_references",
                }
                references = inspector.get_foreign_keys("discussion_picture_references")
                assert any(
                    fk["constrained_columns"] == ["message_id", "revision"]
                    and fk["referred_table"] == "discussion_message_revisions"
                    and fk["options"]["ondelete"] == "CASCADE"
                    for fk in references
                )
                assert {item["name"] for item in inspector.get_indexes("discussion_pictures")} == {
                    "ix_discussion_pictures_scope_owner",
                    "ix_discussion_pictures_expiry",
                }
                migration.downgrade()
                assert sa.inspect(connection).get_table_names() == []
                migration.upgrade()
    finally:
        engine.dispose()

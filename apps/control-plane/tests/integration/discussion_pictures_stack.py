"""Picture HTTP proof using disposable PostgreSQL and an actual MinIO service.

First run discussions_postgres.py on an empty localhost collaboration_test DB.
Then set COLLABORATION_TEST_DATABASE_URL and PICTURE_TEST_MINIO_ENDPOINT for
this script. MinIO must use the synthetic credentials below. No live service,
bucket or vault is used; a new random test bucket is removed in finally.
"""

import hashlib
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from minio import Minio
from PIL import Image, PngImagePlugin
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers import discussion_pictures, discussions
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_db
from app.schemas.discussions import CommandEnvelope
from app.services import discussion_picture_cleanup as cleanup
from app.services import discussion_pictures as pictures
from app.services import discussion_service, session_service


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    endpoint = os.environ["PICTURE_TEST_MINIO_ENDPOINT"]
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only the disposable localhost collaboration_test DB is allowed")
    host, port = endpoint.split(":")
    if host != "127.0.0.1" or not 1024 <= int(port) <= 65535:
        raise RuntimeError("Only a disposable loopback object store is allowed")
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611060005"
    settings = get_settings()
    settings.collaboration_policy_enabled = True
    settings.collaboration_discussions_enabled = True
    settings.collaboration_pictures_enabled = True
    settings.minio_endpoint = endpoint
    settings.minio_access_key = "picture-test"
    settings.minio_secret_key = "disposable-picture-test-password"
    settings.minio_secure = False
    settings.collaboration_pictures_bucket = "discussion-pictures-test-" + uuid.uuid4().hex
    minio = Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    assert not minio.bucket_exists(settings.collaboration_pictures_bucket)
    minio.make_bucket(settings.collaboration_pictures_bucket)
    try:
        exercise(engine, minio, settings)
    finally:
        for obj in minio.list_objects(settings.collaboration_pictures_bucket, recursive=True):
            minio.remove_object(settings.collaboration_pictures_bucket, obj.object_name)
        minio.remove_bucket(settings.collaboration_pictures_bucket)
        engine.dispose()


def exercise(engine, minio, settings):
    with Session(engine, autoflush=False) as db:
        suffix = uuid.uuid4().hex
        users = {
            name: m.User(email=f"{name}-{suffix}@pictures.test", password_hash="", is_active=True)
            for name in ("owner", "reviewer", "reader")
        }
        db.add_all(users.values())
        db.flush()
        share = m.Share(
            kind=m.ShareKind.FOLDER,
            path="Pictures fixture",
            managed=True,
            visibility=m.ShareVisibility.PRIVATE,
            owner_user_id=users["owner"].id,
        )
        db.add(share)
        db.flush()
        entry = m.ManagedEntry(
            id=str(uuid.uuid4()),
            share_id=share.id,
            path="Example.md",
            record={"type": "markdown"},
            deleted=False,
        )
        db.add(entry)
        tokens = {}
        for name, user in users.items():
            session, _ = session_service.create_session(db, user.id)
            tokens[name] = security.create_access_token(str(user.id), session_id=str(session.id))
            if name != "owner":
                db.add(
                    m.ShareMember(share_id=share.id, user_id=user.id, role=m.ShareMemberRole.VIEWER)
                )
                db.add(
                    m.ManagedRule(
                        share_id=share.id,
                        user_id=user.id,
                        path="",
                        recursive=True,
                        permission="read",
                    )
                )
        db.add(
            m.ManagedCommentRule(
                share_id=share.id,
                user_id=users["reviewer"].id,
                path="",
                recursive=True,
                allowed=True,
            )
        )
        db.flush()
        share_id, entry_id, reviewer_id, reader_id = (
            share.id,
            entry.id,
            users["reviewer"].id,
            users["reader"].id,
        )
        db.commit()

    app = FastAPI()
    app.include_router(discussions.router, prefix="/v1")
    app.include_router(discussion_pictures.router, prefix="/v1")

    def database():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[get_db] = database
    base = f"/v1/collaboration/shares/{share_id}/entries/{entry_id}"
    headers = {name: {"Authorization": "Bearer " + token} for name, token in tokens.items()}
    data = BytesIO()
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("author", "metadata must not leave staging")
    with Image.new("RGBA", (900, 600), (40, 80, 120, 160)) as image:
        image.save(data, format="PNG", pnginfo=png_info)
    original = data.getvalue()
    pid = str(uuid.uuid4())
    payload = dict(picture_id=pid, sha256=hashlib.sha256(original).hexdigest(), size=len(original))

    class CheckedStore:
        def check_unlocked(self):
            with Session(engine) as guard:
                assert (
                    guard.scalar(
                        select(m.Share).where(m.Share.id == share_id).with_for_update(nowait=True)
                    )
                    is not None
                )

        def put_object(self, *args, **kwargs):
            self.check_unlocked()
            return minio.put_object(*args, **kwargs)

        def get_object(self, *args, **kwargs):
            self.check_unlocked()
            return minio.get_object(*args, **kwargs)

    with (
        patch.object(pictures.storage_client, "get_minio_client", return_value=CheckedStore()),
        TestClient(app) as client,
    ):
        response = client.post(base + "/pictures", json=payload, headers=headers["reviewer"])
        assert response.status_code == 200, response.text
        assert (
            client.post(base + "/pictures", json=payload, headers=headers["reviewer"]).json()
            == response.json()
        )
        assert client.get(base + f"/pictures/{pid}", headers=headers["owner"]).status_code == 404
        response = client.put(
            base + f"/pictures/{pid}/content", content=original, headers=headers["reviewer"]
        )
        assert response.status_code == 200, response.text
        verified = response.json()
        assert verified["state"] == "verified"
        objects = list(minio.list_objects(settings.collaboration_pictures_bucket, recursive=True))
        assert len(objects) == 2
        anonymous = httpx.get(
            f"http://{settings.minio_endpoint}/{settings.collaboration_pictures_bucket}/"
            + objects[0].object_name,
            timeout=5,
        )
        assert anonymous.status_code == 403
        assert (
            client.put(
                base + f"/pictures/{pid}/content", content=original, headers=headers["reviewer"]
            ).json()
            == verified
        )
        assert (
            client.get(base + f"/pictures/{pid}/content", headers=headers["reader"]).status_code
            == 404
        )
        payload = dict(
            protocol_version=1,
            operation_id=str(uuid.uuid4()),
            share_id=str(share_id),
            entry_id=entry_id,
            command=dict(
                type="thread.create",
                thread_id=str(uuid.uuid4()),
                message_id=str(uuid.uuid4()),
                anchor={"version": 1, "type": "note"},
                body={
                    "version": 1,
                    "blocks": [{"type": "picture", "picture_id": pid, "alt": "A fixture"}],
                },
            ),
        )

        def send():
            with Session(engine, autoflush=False) as db:
                return discussion_service.apply_command(
                    db, tokens["reviewer"], CommandEnvelope.model_validate(payload)
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: send(), range(2)))
        assert first == second
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(m.DiscussionPictureReference)
                    .where(m.DiscussionPictureReference.picture_id == uuid.UUID(pid))
                )
                == 1
            )
        response = client.get(base + f"/pictures/{pid}/content", headers=headers["reader"])
        assert response.status_code == 200, response.text
        assert b"metadata must not leave staging" not in response.content
        assert response.headers["cache-control"] == "private, no-store"
        with Image.open(BytesIO(response.content)) as decoded:
            assert decoded.size == (900, 600)
            assert decoded.getpixel((0, 0)) == (40, 80, 120, 160)
        assert (
            client.get(base + f"/pictures/{pid}/thumbnail", headers=headers["reader"]).status_code
            == 200
        )
        print(
            "PASS: real PostgreSQL/MinIO HTTP staging, sanitized content, thumbnails, "
            "private publication, exact retries and concurrent receipt/reference atomicity"
        )

        original_get = pictures.get_rendition

        def revoke_after_read(rendition):
            content = original_get(rendition)
            with Session(engine) as revoker:
                rule = revoker.scalar(
                    select(m.ManagedRule).where(
                        m.ManagedRule.share_id == share_id, m.ManagedRule.user_id == reader_id
                    )
                )
                rule.permission = "deny"
                revoker.commit()
            return content

        with patch.object(pictures, "get_rendition", side_effect=revoke_after_read):
            assert (
                client.get(base + f"/pictures/{pid}/content", headers=headers["reader"]).status_code
                == 404
            )
        print(
            "PASS: actual object-store operations hold no share lock; "
            "a committed PostgreSQL revoke during download prevents delivery"
        )
        check_cleanup(
            engine, minio, client, base, headers, tokens, share_id, entry_id, original, pid
        )


def check_cleanup(
    engine, minio, client, base, headers, tokens, share_id, entry_id, original, live_pid
):
    settings = get_settings()
    bucket = settings.collaboration_pictures_bucket
    settings.collaboration_picture_cleanup_enabled = True
    factory = sessionmaker(engine, autoflush=False)

    def wake():
        with factory.begin() as db:
            row = db.get(m.DiscussionPictureSweep, bucket)
            if row:
                row.next_run_at = security.utcnow() - timedelta(seconds=1)

    def stage():
        pid = uuid.uuid4()
        response = client.post(
            base + "/pictures",
            headers=headers["reviewer"],
            json=dict(
                picture_id=str(pid),
                sha256=hashlib.sha256(original).hexdigest(),
                size=len(original),
            ),
        )
        assert response.status_code == 200, response.text
        return pid

    def expire(pid):
        with factory.begin() as db:
            db.get(m.DiscussionPicture, pid).expires_at = security.utcnow() - timedelta(seconds=1)

    class CheckedCleanupStore:
        after_put = None
        before_delete = None

        def unlocked(self):
            # Both the authorization lock and maintenance lease must be free
            # throughout storage I/O, on independent PostgreSQL connections.
            with factory.begin() as guard:
                assert (
                    guard.scalar(
                        select(m.Share)
                        .where(
                            m.Share.id == share_id,
                        )
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
                guard.scalar(
                    select(m.DiscussionPictureSweep)
                    .where(
                        m.DiscussionPictureSweep.bucket == bucket,
                    )
                    .with_for_update(nowait=True)
                )

        def get_bucket_versioning(self, *args, **kwargs):
            self.unlocked()
            return minio.get_bucket_versioning(*args, **kwargs)

        def list_objects(self, *args, **kwargs):
            self.unlocked()
            for obj in minio.list_objects(*args, **kwargs):
                self.unlocked()
                yield obj

        def remove_object(self, *args, **kwargs):
            self.unlocked()
            if self.before_delete:
                self.before_delete(*args)
            return minio.remove_object(*args, **kwargs)

        def put_object(self, *args, **kwargs):
            self.unlocked()
            result = minio.put_object(*args, **kwargs)
            if self.after_put:
                callback, self.after_put = self.after_put, None
                callback()
            return result

    store = CheckedCleanupStore()
    with patch.object(pictures.storage_client, "get_minio_client", return_value=store):
        # An actual upload remains in-flight while maintenance expires it and
        # removes the first rendition. The second PUT arrives after that sweep.
        pid = stage()
        first_written, resume = threading.Event(), threading.Event()

        def pause():
            first_written.set()
            assert resume.wait(10), "cleanup proof timed out"

        def upload():
            with factory() as db:
                ticket = pictures.prepare_upload(db, tokens["reviewer"], share_id, entry_id, pid)
                return pictures.upload(db, tokens["reviewer"], share_id, entry_id, ticket, original)

        store.after_put = pause
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(upload)
            try:
                assert first_written.wait(5)
                expire(pid)
                counts = cleanup.Sweep(factory).run()
                assert counts.expired == 1 and counts.deleted == 1 and counts.completed
            finally:
                resume.set()
            try:
                future.result(timeout=5)
                raise AssertionError("Expired upload finalized")
            except HTTPException as error:
                assert error.status_code == 410
        wake()
        assert cleanup.Sweep(factory).run().deleted == 1
        with factory() as db:
            live = db.get(m.DiscussionPicture, uuid.UUID(live_pid))
            live_keys = {r["key"] for r in live.renditions.values()}
        assert {o.object_name for o in minio.list_objects(bucket, recursive=True)} == live_keys

        # A foreground writer must not be blocked by cleanup and must not be
        # mistaken for a missing share when SKIP LOCKED returns no row.
        wake()
        sweep = cleanup.Sweep(factory)
        assert sweep.claim()
        with factory.begin() as competing:
            competing.scalar(
                select(m.DiscussionPictureSweep)
                .where(
                    m.DiscussionPictureSweep.bucket == bucket,
                )
                .with_for_update()
            )
            try:
                sweep.heartbeat()
                raise AssertionError("Maintenance waited on or bypassed a busy lease")
            except cleanup.Stopped:
                pass
        obj = next(iter(minio.list_objects(bucket, recursive=True)))
        with factory.begin() as foreground:
            foreground.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
            assert not sweep.classify(obj, cleanup.identify(obj.object_name))
            assert sweep.counts.deferred == 1
        sweep.release()

        # Once a missing identity is retired, stage cannot recreate it even
        # while the old objects are being physically deleted without SQL locks.
        abandoned = stage()
        with factory.begin() as db:
            picture = db.get(m.DiscussionPicture, abandoned)
            prefix = pictures.namespace(share_id, picture.scope_id, abandoned)
            db.delete(picture)
        key = prefix + "v1/" + "1" * 64 + ".png"
        minio.put_object(bucket, key, BytesIO(original), len(original))
        wake()
        assert cleanup.Sweep(factory).run().deleted == 0
        with factory.begin() as db:
            retirement = db.get(m.DiscussionPictureRetirement, (bucket, prefix))
            retirement.first_seen_at = security.utcnow() - timedelta(days=31)

        def reuse(_bucket, _key):
            response = client.post(
                base + "/pictures",
                headers=headers["reviewer"],
                json=dict(
                    picture_id=str(abandoned),
                    sha256=hashlib.sha256(original).hexdigest(),
                    size=len(original),
                ),
            )
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "identifier_unavailable"

        store.before_delete = reuse
        wake()
        assert cleanup.Sweep(factory).run().deleted == 1
        store.before_delete = None

        # Competing workers agree on exactly one durable lease.
        wake()
        barrier = threading.Barrier(2)
        sweeps = [cleanup.Sweep(factory), cleanup.Sweep(factory)]

        def claim(sweep):
            barrier.wait(timeout=5)
            return sweep.claim()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(claim, sweeps))
        assert sorted(claimed) == [False, True]
        for sweep, owns in zip(sweeps, claimed):
            if owns:
                sweep.release()
        assert {o.object_name for o in minio.list_objects(bucket, recursive=True)} == live_keys
    print(
        "PASS: actual PostgreSQL/MinIO expiry before deletion, late PUT reconciliation, "
        "live history protection, nonblocking share locks, retired-ID fencing and competing leases"
    )
    for _ in range(4):
        command = CommandEnvelope.model_validate(
            dict(
                protocol_version=1,
                operation_id=uuid.uuid4(),
                share_id=share_id,
                entry_id=entry_id,
                command=dict(
                    type="thread.create",
                    thread_id=uuid.uuid4(),
                    message_id=uuid.uuid4(),
                    anchor=dict(version=1, type="note"),
                    body=dict(
                        version=1,
                        blocks=[
                            dict(
                                type="paragraph", children=[dict(type="text", text="Recovery race")]
                            )
                        ],
                    ),
                ),
            )
        )
        start = threading.Barrier(2)

        def compete(withdraw):
            with factory() as db:
                start.wait(timeout=5)
                try:
                    call = (
                        discussion_service.withdraw_command
                        if withdraw
                        else discussion_service.apply_command
                    )
                    return call(db, tokens["reviewer"], command)
                except HTTPException as exc:
                    assert not withdraw and exc.status_code == 410
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            sent, withdrawn = list(pool.map(compete, [False, True]))
        assert withdrawn["status"] == ("accepted" if sent else "withdrawn")
        with factory() as db:
            assert (db.get(m.DiscussionMessage, command.command.message_id) is not None) == bool(
                sent
            )
            if sent:
                assert withdrawn["result"] == sent
            else:
                try:
                    discussion_service.apply_command(db, tokens["reviewer"], command)
                    raise AssertionError("A late send bypassed withdrawal")
                except HTTPException as error:
                    assert error.status_code == 410
    print(
        "PASS: concurrent PostgreSQL send/withdraw returns acceptance "
        "or permanently fences the original"
    )


if __name__ == "__main__":
    main()

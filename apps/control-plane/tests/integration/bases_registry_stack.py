"""Bases HTTP/concurrency proof using disposable PostgreSQL and MinIO.

Run discussions_postgres.py first on an empty localhost collaboration_test DB.
Only synthetic accounts and a new random bucket are used; finally removes both.
"""

import hashlib
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.routers import bases
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_db
from app.schemas.bases import InspectBase, ReconcileBase
from app.services import bases_registry as service
from app.services import session_service


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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611060003"
    settings = get_settings()
    settings.collaboration_policy_enabled = settings.collaboration_bases_registry_enabled = True
    settings.minio_endpoint = endpoint
    settings.minio_access_key, settings.minio_secret_key = (
        "picture-test",
        "disposable-picture-test-password",
    )
    settings.minio_secure = False
    settings.minio_bucket = "bases-registry-test-" + uuid.uuid4().hex
    minio = Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    assert not minio.bucket_exists(settings.minio_bucket)
    minio.make_bucket(settings.minio_bucket)
    users, share_id = [], None
    try:
        with Session(engine, autoflush=False) as db:
            owner = m.User(
                email=f"owner-{uuid.uuid4().hex}@bases.test", password_hash="", is_active=True
            )
            reviewer = m.User(
                email=f"reviewer-{uuid.uuid4().hex}@bases.test", password_hash="", is_active=True
            )
            db.add_all([owner, reviewer])
            db.flush()
            users = [owner.id, reviewer.id]
            share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Bases fixture",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=owner.id,
            )
            db.add(share)
            db.flush()
            share_id = share.id
            entry = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=share_id,
                path="Planning.base",
                record={"type": "file"},
                deleted=False,
            )
            db.add(entry)
            db.add(
                m.ShareMember(share_id=share_id, user_id=reviewer.id, role=m.ShareMemberRole.VIEWER)
            )
            db.add(
                m.ManagedRule(
                    share_id=share_id,
                    user_id=reviewer.id,
                    path="",
                    recursive=True,
                    permission="read",
                )
            )
            tokens = {}
            for name, user in (("owner", owner), ("reviewer", reviewer)):
                session, _ = session_service.create_session(db, user.id)
                tokens[name] = security.create_access_token(
                    str(user.id), session_id=str(session.id)
                )
            entry_id, reviewer_id = entry.id, reviewer.id
            db.commit()

        key = f"web-assets/{share_id}/{entry_id}"

        def publish(views):
            raw = json.dumps({"views": views}).encode()
            sha256 = hashlib.sha256(raw).hexdigest()
            minio.put_object(
                settings.minio_bucket, key, BytesIO(raw), len(raw), content_type="application/yaml"
            )
            with Session(engine) as db:
                db.get(m.ManagedEntry, entry_id).record = {"type": "file", "hash": sha256}
                db.commit()
            return sha256

        sha256 = publish([dict(name="Table", type="table"), dict(name="Cards", type="cards")])
        barrier = threading.Barrier(2)
        reads = []
        probes = threading.Lock()

        class CheckedStorage:
            concurrent = True
            after_read = None

            def get_object(self, *args):
                if self.concurrent:
                    barrier.wait(timeout=10)
                with probes, Session(engine) as guard:
                    assert (
                        guard.scalar(
                            select(m.Share)
                            .where(m.Share.id == share_id)
                            .with_for_update(nowait=True)
                        )
                        is not None
                    )
                response = minio.get_object(*args)
                reads.append(args)
                if self.concurrent:
                    barrier.wait(timeout=10)
                if self.after_read:
                    callback, self.after_read = self.after_read, None
                    callback()
                return response

        storage = CheckedStorage()

        def call(payload, actor="reviewer"):
            with Session(engine, autoflush=False) as db:
                try:
                    return 200, service.synchronize(
                        db, tokens[actor], share_id, uuid.UUID(entry_id), payload
                    )
                except HTTPException as exc:
                    return exc.status_code, exc.detail

        with patch.object(service.storage_client, "get_minio_client", return_value=storage):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(call, [InspectBase(content_sha256=sha256)] * 2))
            assert outcomes[0] == outcomes[1] and outcomes[0][0] == 200, outcomes
            first = outcomes[0][1]
            with Session(engine) as db:
                assert db.scalar(select(func.count()).select_from(m.BaseDefinition)) == 1
                assert db.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == 1
            print(
                "PASS: concurrent read-only inspection creates one registry; "
                "real MinIO reads hold no SQL lock"
            )

            storage.concurrent = False
            app = FastAPI()
            app.include_router(bases.router, prefix="/v1")

            def database():
                with Session(engine, autoflush=False) as db:
                    yield db

            app.dependency_overrides[get_db] = database
            route = f"/v1/collaboration/shares/{share_id}/entries/{entry_id}/bases"
            auth = {name: {"Authorization": "Bearer " + token} for name, token in tokens.items()}
            with TestClient(app) as client:
                sha256 = publish(
                    [dict(name="Cards", type="cards"), dict(name="Renamed", type="table")]
                )
                response = client.post(
                    route + "/inspect", headers=auth["reviewer"], json=dict(content_sha256=sha256)
                )
                assert response.status_code == 200, response.text
                renamed = response.json()
                assert [v["id"] for v in renamed["views"]] == [
                    v["id"] for v in reversed(first["views"])
                ]
                sha256 = publish([dict(name="Calendar", type="sabidurian-timeline")])
                response = client.post(
                    route + "/inspect", headers=auth["reviewer"], json=dict(content_sha256=sha256)
                )
                assert response.status_code == 200, response.text
                ambiguous = response.json()
                assert (
                    ambiguous["views"][0]["id"] is None
                    and len(ambiguous["unmatched"]["views"]) == 2
                )
                payload = dict(
                    content_sha256=sha256,
                    expected_revision=ambiguous["revision"],
                    operation_id=str(uuid.uuid4()),
                    bindings=[dict(slot=0, view_id=first["views"][0]["id"])],
                    archive=[first["views"][1]["id"]],
                )
                assert (
                    client.post(
                        route + "/reconcile", headers=auth["reviewer"], json=payload
                    ).status_code
                    == 403
                )
                a = ReconcileBase.model_validate(payload)
                b = a.model_copy(update={"operation_id": uuid.uuid4()})
                storage.concurrent = True
                barrier = threading.Barrier(2)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(lambda p: call(p, "owner"), [a, b]))
                assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
                storage.concurrent = False
                winner = a if outcomes[0][0] == 200 else b
                before_retry = len(reads)
                assert call(winner, "owner") == next(
                    outcome for outcome in outcomes if outcome[0] == 200
                )
                assert len(reads) == before_retry
                print(
                    "PASS: HTTP rename/ambiguity, editor-only repair, "
                    "concurrent CAS winner and exact receipt replay"
                )

                sha256 = publish([dict(name="Another", type="list")])

                def revoke():
                    with Session(engine) as db:
                        db.scalar(
                            select(m.ManagedRule).where(
                                m.ManagedRule.share_id == share_id,
                                m.ManagedRule.user_id == reviewer_id,
                            )
                        ).permission = "none"
                        db.commit()

                storage.after_read = revoke
                assert call(InspectBase(content_sha256=sha256))[0] == 404
                assert (
                    client.get(
                        route + "/views?state=archived", headers=auth["reviewer"]
                    ).status_code
                    == 404
                )
                print(
                    "PASS: revocation during actual object read prevents "
                    "registry publication and later history access"
                )
    finally:
        with Session(engine) as db:
            if share_id:
                share = db.get(m.Share, share_id)
                if share:
                    db.delete(share)
            for user_id in users:
                user = db.get(m.User, user_id)
                if user:
                    db.delete(user)
            db.commit()
            assert db.scalar(select(func.count()).select_from(m.BaseDefinition)) == 0
            assert db.scalar(select(func.count()).select_from(m.BaseView)) == 0
            assert db.scalar(select(func.count()).select_from(m.BaseRegistryRevision)) == 0
        for obj in minio.list_objects(settings.minio_bucket, recursive=True):
            minio.remove_object(settings.minio_bucket, obj.object_name)
        minio.remove_bucket(settings.minio_bucket)
        engine.dispose()
    print("PASS: share deletion cascades registry, views and receipts; disposable bucket removed")


if __name__ == "__main__":
    main()

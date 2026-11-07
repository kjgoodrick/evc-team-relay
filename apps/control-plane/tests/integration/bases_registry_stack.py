"""Bases HTTP/concurrency proof using disposable PostgreSQL and MinIO.

Run discussions_postgres.py first on an empty localhost collaboration_test DB.
Only synthetic accounts and a new random bucket are used; finally removes both.
"""

import base64
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

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.routers import base_content as content_router
from app.api.routers import bases
from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.db.session import get_db
from app.schemas.base_content import PublishBaseContent
from app.schemas.bases import BaseTargetCatalog, InspectBase, ReconcileBase
from app.services import base_content, base_target_definitions, managed_service, session_service
from app.services import bases_registry as service


def prove_target_definitions(engine, client, route, auth, publish, share_id, entry_id, tokens):
    settings = get_settings()
    settings.collaboration_discussions_enabled = (
        settings.collaboration_bases_discussions_enabled
    ) = True
    views = [
        dict(
            name="Table",
            type="table",
            groupBy=dict(property="status", direction="ASC"),
            summaries={"formula.total": "Average"},
        ),
        dict(name="Cards", type="cards"),
    ]

    def inspect_def(formulas):
        sha = publish(views, formulas=formulas)
        response = client.post(
            route + "/inspect", headers=auth["owner"], json=dict(content_sha256=sha)
        )
        assert response.status_code == 200, response.text
        return response.json()

    snapshot = inspect_def(dict(total="price * 2"))
    view_id = snapshot["views"][0]["id"]
    payload = BaseTargetCatalog(view_id=view_id)
    first = client.post(
        route + "/target-definitions",
        headers=auth["reviewer"],
        json=payload.model_dump(mode="json"),
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["targets"]) == 3
    assert first.json()["computed_values"] is False
    with Session(engine) as db:
        # Simulate the lazy initialization of an already-existing registry.
        for row in db.scalars(select(m.BaseTargetDefinition)):
            db.delete(row)
        db.commit()
    barrier = threading.Barrier(2)

    def read_catalog():
        with Session(engine, autoflush=False) as db:
            barrier.wait(timeout=10)
            return base_target_definitions.catalog(
                db, tokens["reviewer"], share_id, uuid.UUID(entry_id), payload
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = [
            future.result() for future in [pool.submit(read_catalog), pool.submit(read_catalog)]
        ]
    assert left == right
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(m.BaseTargetDefinition)) == 4
    print("PASS: concurrent read-only semantic catalogs create one identity per view/definition")

    inspect_def(dict(total="price * 3"))
    inspect_def(dict(total="price * 2"))
    restored = client.post(
        route + "/target-definitions",
        headers=auth["reviewer"],
        json=payload.model_dump(mode="json"),
    ).json()
    before = {item["id"]: item for item in left["targets"]}
    for item in restored["targets"]:
        assert item["fingerprint"] == before[item["id"]]["fingerprint"]
        assert (item["revision"] > before[item["id"]]["revision"]) == (item["kind"] != "group")
    with Session(engine) as db:
        share = db.scalar(select(m.Share).where(m.Share.id == share_id).with_for_update())
        db.add(
            m.ManagedRule(
                share_id=share.id,
                user_id=db.scalar(
                    select(m.ShareMember.user_id).where(m.ShareMember.share_id == share.id)
                ),
                path="Planning.base",
                recursive=False,
                permission="none",
            )
        )
        db.commit()
    denied = client.post(
        route + "/target-definitions",
        headers=auth["reviewer"],
        json=payload.model_dump(mode="json"),
    )
    assert denied.status_code == 404, denied.text
    with Session(engine) as db:
        rule = db.scalar(
            select(m.ManagedRule).where(
                m.ManagedRule.share_id == share_id, m.ManagedRule.path == "Planning.base"
            )
        )
        db.delete(rule)
        db.commit()
    settings.database_url = engine.url.render_as_string(hide_password=False)
    try:
        command.downgrade(Config("alembic.ini"), "202611060004")
    except RuntimeError as exc:
        assert "Export/migrate Bases target identities" in str(exc)
    else:
        raise AssertionError("Nonempty semantic identities must prevent destructive downgrade")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611060007"
    print(
        "PASS: semantic edit/revert revisions, current read revocation "
        "and nonempty downgrade refusal"
    )
    # Return the descriptors expected by the subsequent rename/repair proof.
    views[:] = [dict(name="Table", type="table"), dict(name="Cards", type="cards")]
    inspect_def({})


def prove_content(engine, url, client, share_id, entry_id, tokens, minio, key):
    settings = get_settings()
    settings.managed_bases_sync_enabled = True
    route = f"/v1/managed/shares/{share_id}/entries/{entry_id}/base-content"
    auth = {"Authorization": "Bearer " + tokens["owner"]}
    before = client.get(route, headers=auth)
    assert before.status_code == 200, before.text
    assert base64.b64decode(before.json()["content_base64"])

    def payload(state, raw):
        return PublishBaseContent.model_validate(
            dict(
                operation_id=str(uuid.uuid4()),
                expected=state,
                content_base64=base64.b64encode(raw).decode(),
            )
        )

    barrier = threading.Barrier(2)

    def publish(p):
        with Session(engine, autoflush=False) as db:
            barrier.wait(timeout=10)
            try:
                return 200, base_content.publish(
                    db, tokens["owner"], share_id, uuid.UUID(entry_id), p
                )
            except HTTPException as exc:
                return exc.status_code, exc.detail

    bodies = [
        payload(before.json()["state"], raw)
        for raw in (b"views: []\n", b"views: []\n# competing edit\n")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, bodies))
    assert sorted(code for code, _ in outcomes) == [200, 409], outcomes
    winner = outcomes[0][1] if outcomes[0][0] == 200 else outcomes[1][1]
    winner_body = bodies[0] if outcomes[0][0] == 200 else bodies[1]
    stored = client.get(route, headers=auth).json()
    assert stored["state"] == winner["state"]
    assert stored["content_base64"] == winner_body.content_base64
    assert (
        client.put(route, headers=auth, json=winner_body.model_dump(mode="json")).json() == winner
    )
    # Late legacy writes cannot replace guarded content, even if already in flight
    # when the first publication selected PostgreSQL as its authoritative source.
    minio.put_object(settings.minio_bucket, key, BytesIO(b"late legacy upload"), 18)
    assert client.get(route, headers=auth).json() == stored
    print(
        "PASS: competing Bases publications have one winner; content/index commit together; "
        "late legacy bytes cannot replace it"
    )

    duplicate = payload(stored["state"], b"views: []\n# identical request\n")
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, [duplicate, duplicate]))
    assert outcomes[0] == outcomes[1] and outcomes[0][0] == 200, outcomes
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(m.ManagedBaseReceipt)
                .where(m.ManagedBaseReceipt.entry_id == entry_id)
            )
            == 2
        )
    current = outcomes[0][1]["state"]
    print("PASS: concurrent duplicate publication returns one receipt and one content version")

    # Unknown delivery is resolved under the same share lock as publication.
    racing = payload(current, b"views: []\n# send versus withdrawal\n")
    barrier = threading.Barrier(2)

    def withdraw():
        with Session(engine, autoflush=False) as db:
            barrier.wait(timeout=10)
            return base_content.withdraw(db, tokens["owner"], share_id, uuid.UUID(entry_id), racing)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(publish, racing), pool.submit(withdraw)
        sent, recovery = a.result(), b.result()
    if recovery["status"] == "accepted":
        assert sent == (200, recovery["result"])
    else:
        assert recovery["status"] == "withdrawn" and sent[0] == 410, (sent, recovery)
        response = client.put(route, headers=auth, json=racing.model_dump(mode="json"))
        assert response.status_code == 410
    current = client.get(route, headers=auth).json()["state"]
    print(
        "PASS: concurrent Bases send/withdraw returns acceptance or permanently fences "
        "late publication"
    )

    # Publication and structural rename share the same serialization boundary.
    edit = payload(current, b"views: []\n# edit versus rename\n")
    barrier = threading.Barrier(2)

    def rename():
        with Session(engine, autoflush=False) as db:
            share = db.get(m.Share, share_id)
            owner = db.get(m.User, share.owner_user_id)
            # Deliberately cache an entry before the competing publication.
            cached_entry = db.get(m.ManagedEntry, entry_id)
            assert cached_entry is not None
            barrier.wait(timeout=10)
            try:
                managed_service.apply_batch(
                    db,
                    share,
                    owner,
                    managed_service.Batch.model_validate(
                        {
                            "changes": [
                                {
                                    "path": current["path"],
                                    "previous": current["record"],
                                    "value": None,
                                },
                                {
                                    "path": "Renamed.base",
                                    "previous": None,
                                    "value": current["record"],
                                },
                            ]
                        }
                    ),
                )
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(publish, edit), pool.submit(rename)
        codes = [a.result()[0], b.result()]
    assert sorted(codes) == [200, 409], codes
    current = client.get(route, headers=auth).json()
    with Session(engine) as db:
        entry = db.get(m.ManagedEntry, entry_id)
        content = db.get(m.ManagedBaseContent, entry_id)
        assert entry.record["hash"] == hashlib.sha256(content.data).hexdigest()
        assert current["state"]["record"] == entry.record
    print(
        "PASS: concurrent rename versus publication cannot apply stale inventory "
        "over guarded content"
    )

    settings.database_url = url.render_as_string(hide_password=False)
    try:
        command.downgrade(Config("alembic.ini"), "202611060003")
    except RuntimeError as exc:
        assert "Export/migrate" in str(exc)
    else:
        raise AssertionError("Nonempty guarded content must prevent destructive downgrade")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611060007"
    assert client.get(route, headers=auth).json() == current
    print("PASS: nonempty downgrade is refused and leaves the version and bytes intact")


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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202611060007"
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

        def publish(views, **extra):
            raw = json.dumps({"views": views, **extra}).encode()
            sha256 = hashlib.sha256(raw).hexdigest()
            minio.put_object(
                settings.minio_bucket, key, BytesIO(raw), len(raw), content_type="application/yaml"
            )
            with Session(engine) as db:
                db.get(m.ManagedEntry, entry_id).record = {
                    "id": entry_id,
                    "version": 0,
                    "type": "file",
                    "hash": sha256,
                }
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
            app.include_router(content_router.router, prefix="/v1")

            def database():
                with Session(engine, autoflush=False) as db:
                    yield db

            app.dependency_overrides[get_db] = database
            route = f"/v1/collaboration/shares/{share_id}/entries/{entry_id}/bases"
            auth = {name: {"Authorization": "Bearer " + token} for name, token in tokens.items()}
            with TestClient(app) as client:
                prove_target_definitions(
                    engine, client, route, auth, publish, share_id, entry_id, tokens
                )
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
                prove_content(engine, url, client, share_id, entry_id, tokens, minio, key)
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
            assert db.scalar(select(func.count()).select_from(m.BaseTargetDefinition)) == 0
            assert db.scalar(select(func.count()).select_from(m.ManagedBaseContent)) == 0
            assert db.scalar(select(func.count()).select_from(m.ManagedBaseReceipt)) == 0
        for obj in minio.list_objects(settings.minio_bucket, recursive=True):
            minio.remove_object(settings.minio_bucket, obj.object_name)
        minio.remove_bucket(settings.minio_bucket)
        engine.dispose()
    print("PASS: share deletion cascades registry, views and receipts; disposable bucket removed")


if __name__ == "__main__":
    main()

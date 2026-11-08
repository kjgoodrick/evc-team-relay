"""Exercise the actual plugin Bases transport/store over disposable loopback HTTP.

SQLite proves the client wire/persistence contract; bases_registry_stack.py
separately proves PostgreSQL serialization. No vault or existing service is used.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from managed_stack import port, ready, stop

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument(
        "--restore-guard",
        action="store_true",
        help="Verify bound Bases content, saved-view repairs and markers after restore",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="evc-base-content-client-") as temporary:
        temp, number = Path(temporary), port()
        os.environ.update(
            DATABASE_URL="sqlite+pysqlite:///" + str(temp / "test.db"),
            JWT_SECRET=secrets.token_hex(32),
            MANAGED_BASES_SYNC_ENABLED="true",
            COLLABORATION_POLICY_ENABLED="true",
            COLLABORATION_PRESENCE_ENABLED="true",
            COLLABORATION_BASES_REGISTRY_ENABLED="true",
            COLLABORATION_DISCUSSIONS_ENABLED="true",
            COLLABORATION_BASES_DISCUSSIONS_ENABLED="true",
            COLLABORATION_BASES_DEFINITION_TARGETS_ENABLED="true",
            COLLABORATION_BASES_FORMULA_CELLS_ENABLED="true",
            COLLABORATION_BASES_TIMELINE_RANGES_ENABLED="true",
            COLLABORATION_BASES_TIMELINE_MARKERS_ENABLED="true",
            LOG_LEVEL="WARNING",
            CONTROL_PLANE_PUBLIC_URL=f"http://127.0.0.1:{number}",
        )
        from app.core import security
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.db import models as m
        from app.db import session
        from app.services import session_service

        m.Base.metadata.create_all(session.get_engine())
        with session.get_sessionmaker()() as db:
            owner = m.User(email="owner@bases.fixture", password_hash="", is_active=True)
            reviewer = m.User(email="reviewer@bases.fixture", password_hash="", is_active=True)
            db.add_all([owner, reviewer])
            db.flush()
            share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Team",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=owner.id,
                collaboration_restore_generation=uuid.uuid4() if args.restore_guard else None,
            )
            db.add(share)
            db.flush()
            entry_id = str(uuid.uuid4())
            registry_entry_id = str(uuid.uuid4())
            discussion_entry_id, record_id = str(uuid.uuid4()), str(uuid.uuid4())
            session_entry_id = str(uuid.uuid4())
            definitions_entry_id = str(uuid.uuid4())
            markers_entry_id = str(uuid.uuid4())
            image_record_id = str(uuid.uuid4())
            db.add_all(
                [
                    m.ManagedEntry(
                        id=entry_id,
                        share_id=share.id,
                        path="Planning.base",
                        deleted=False,
                        record={"id": entry_id, "version": 0, "type": "file"},
                    ),
                    m.ManagedEntry(
                        id=registry_entry_id,
                        share_id=share.id,
                        path="Registry.base",
                        deleted=False,
                        record={"id": registry_entry_id, "version": 0, "type": "file"},
                    ),
                    m.ShareMember(
                        share_id=share.id, user_id=reviewer.id, role=m.ShareMemberRole.VIEWER
                    ),
                    m.ManagedRule(
                        share_id=share.id,
                        user_id=reviewer.id,
                        path="",
                        recursive=True,
                        permission="read",
                    ),
                    m.ManagedCommentRule(
                        share_id=share.id,
                        user_id=reviewer.id,
                        path="",
                        recursive=True,
                        allowed=True,
                    ),
                ]
            )
            for identity, path, kind in [
                (discussion_entry_id, "Discussions.base", "file"),
                (session_entry_id, "Sessions.base", "file"),
                (definitions_entry_id, "Definitions.base", "file"),
                (markers_entry_id, "Markers.base", "file"),
                (record_id, "Record.md", "markdown"),
                (image_record_id, "Image.png", "image"),
            ]:
                db.add(
                    m.ManagedEntry(
                        id=identity,
                        share_id=share.id,
                        path=path,
                        deleted=False,
                        record={"id": identity, "version": 0, "type": kind},
                    )
                )
            owner_session, _ = session_service.create_session(db, owner.id)
            reviewer_session, _ = session_service.create_session(db, reviewer.id)
            presence_share = m.Share(
                kind=m.ShareKind.FOLDER,
                path="Presence",
                managed=True,
                visibility=m.ShareVisibility.PRIVATE,
                owner_user_id=owner.id,
            )
            db.add(presence_share)
            db.flush()
            db.add_all(
                [
                    m.ShareMember(
                        share_id=presence_share.id,
                        user_id=reviewer.id,
                        role=m.ShareMemberRole.VIEWER,
                    ),
                    m.ManagedRule(
                        share_id=presence_share.id,
                        user_id=reviewer.id,
                        path="",
                        recursive=True,
                        permission="read",
                    ),
                ]
            )
            presence_image = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=presence_share.id,
                path="Presence.png",
                record={"type": "image", "hash": "a" * 64},
            )
            presence_base = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=presence_share.id,
                path="Presence.base",
                record={"type": "file", "hash": "b" * 64},
            )
            presence_note = m.ManagedEntry(
                id=str(uuid.uuid4()),
                share_id=presence_share.id,
                path="Presence.md",
                record={"type": "markdown"},
            )
            db.add_all([presence_image, presence_base, presence_note])
            db.flush()
            presence_definition = m.BaseDefinition(
                entry_id=presence_base.id,
                generation=presence_base.collaboration_generation,
                revision=1,
                content_sha256="b" * 64,
                configuration={
                    "views": [
                        {"type": "table", "name": "Table", "groupBy": {"property": "note.status"}},
                        {
                            "type": "sabidurian-timeline",
                            "name": "Timeline",
                            "groupBy": {"property": "note.status"},
                            "startDateProp": "note.start",
                            "timeZone": "America/Denver",
                        },
                    ]
                },
            )
            db.add(presence_definition)
            db.flush()
            presence_views = [
                m.BaseView(
                    definition_id=presence_definition.id,
                    slot=slot,
                    state="active",
                    descriptor=descriptor,
                )
                for slot, descriptor in enumerate(presence_definition.configuration["views"])
            ]
            db.add_all(presence_views)
            db.commit()
            fixture = dict(
                base=f"http://127.0.0.1:{number}",
                share=str(share.id),
                restoreGeneration=str(share.collaboration_restore_generation)
                if args.restore_guard
                else None,
                entry=entry_id,
                registryEntry=registry_entry_id,
                discussionEntry=discussion_entry_id,
                sessionEntry=session_entry_id,
                definitionsEntry=definitions_entry_id,
                markersEntry=markers_entry_id,
                record=record_id,
                imageRecord=image_record_id,
                owner=str(owner.id),
                reviewer=str(reviewer.id),
                ownerToken=security.create_access_token(
                    str(owner.id), session_id=str(owner_session.id)
                ),
                reviewerToken=security.create_access_token(
                    str(reviewer.id), session_id=str(reviewer_session.id)
                ),
                presence=dict(
                    note=dict(
                        kind="note",
                        share_id=str(presence_share.id),
                        entry_id=presence_note.id,
                        surface="editor",
                    ),
                    image=dict(
                        kind="image",
                        share_id=str(presence_share.id),
                        entry_id=presence_image.id,
                        generation=str(presence_image.collaboration_generation),
                        content_sha256="a" * 64,
                    ),
                    views=[
                        dict(
                            kind="base",
                            share_id=str(presence_share.id),
                            entry_id=presence_base.id,
                            generation=str(presence_base.collaboration_generation),
                            definition_id=str(presence_definition.id),
                            view_id=str(view.id),
                            registry_revision=1,
                        )
                        for view in presence_views
                    ],
                ),
            )
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        fixture_path.chmod(0o600)
        process = None
        try:
            with (temp / "server.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(number),
                        "--no-access-log",
                        "--lifespan",
                        "off",
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                ready(number, process)
                result = subprocess.run(
                    [
                        "node",
                        str(args.plugin.resolve() / "node_modules/jest/bin/jest.js"),
                        "--runInBand",
                        *(
                            [
                                "__tests__/collaboration/realTimelineMarkerRestoreStack.test.ts",
                                "__tests__/bases/realBaseRegistry.test.ts",
                                "__tests__/managed/realManagedRestore.test.ts",
                            ]
                            if args.restore_guard
                            else [
                                "__tests__/bases/realBaseContent.test.ts",
                                "__tests__/bases/realBaseRegistry.test.ts",
                                "__tests__/collaboration/realBaseDiscussionStack.test.ts",
                                "__tests__/collaboration/realBaseDefinitionStack.test.ts",
                                "__tests__/collaboration/realTimelineMarkerStack.test.ts",
                                "__tests__/presence/realSurfaceLocationStack.test.ts",
                            ]
                        ),
                    ],
                    cwd=args.plugin.resolve(),
                    env={
                        **os.environ,
                        "EVC_BASE_CONTENT_FIXTURE": str(fixture_path),
                        "EVC_SURFACE_LOCATION_FIXTURE": str(fixture_path),
                    },
                    timeout=90,
                )
                if result.returncode:
                    raise RuntimeError("Bases client HTTP proof failed")
                print(
                    "PASS: restored Bases review, immutable saves, old-work fencing "
                    "and marker retries"
                    if args.restore_guard
                    else "PASS: actual Bases client HTTP, offline persistence, exact retry, "
                    "conflicts, withdrawal, saved-view identity, repair recovery, "
                    "discussions and surface presence"
                )
        finally:
            if process:
                stop(process)
            session.get_engine().dispose()
    print("Disposable HTTP process and SQLite fixture removed; no native vault was used")


if __name__ == "__main__":
    main()

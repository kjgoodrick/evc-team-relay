"""Physical reclamation proof, called only by the disposable client harness."""

from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.core import security
from app.core.config import get_settings
from app.db import models as m
from app.services import discussion_picture_cleanup, discussion_retention


def prove_retention(fixture, factory, store, bucket):
    if urlsplit(fixture["base"]).hostname != "127.0.0.1" or not bucket.startswith(
        "discussion-pictures-client-"
    ):
        raise RuntimeError("Only the disposable loopback picture fixture is supported")
    settings = get_settings()
    with factory() as db:
        removed = list(
            db.scalars(select(m.DiscussionMessage).where(m.DiscussionMessage.deleted.is_(True)))
        )
        assert removed, "Expected a removed fixture message"
        removed_ids = [message.id for message in removed]
        targets = list(
            db.scalars(
                select(m.DiscussionPicture).where(
                    m.DiscussionPicture.id.in_(
                        select(m.DiscussionPictureReference.picture_id).where(
                            m.DiscussionPictureReference.message_id.in_(removed_ids)
                        )
                    )
                )
            )
        )
        target_keys = {
            rendition["key"] for picture in targets for rendition in picture.renditions.values()
        }
        live = list(
            db.scalars(
                select(m.DiscussionPicture).where(
                    m.DiscussionPicture.id.in_(
                        select(m.DiscussionPictureReference.picture_id).where(
                            ~m.DiscussionPictureReference.message_id.in_(removed_ids)
                        )
                    )
                )
            )
        )
        live_keys = {
            rendition["key"] for picture in live for rendition in picture.renditions.values()
        }
        ids = [(str(message.thread_id), str(message.id)) for message in removed]
        assert target_keys and live_keys and not target_keys & live_keys
    before = {obj.object_name for obj in store.list_objects(bucket, recursive=True)}
    assert target_keys <= before and live_keys <= before
    old_purge, old_sweep = (
        settings.collaboration_removed_content_cleanup_enabled,
        settings.collaboration_picture_cleanup_enabled,
    )
    try:
        settings.collaboration_removed_content_cleanup_enabled = True
        settings.collaboration_picture_cleanup_enabled = True
        # The fixture's logical clock advances; no live clock or retention setting changes.
        counts = discussion_retention.run_batch(
            factory=factory, now=security.utcnow() + timedelta(days=31)
        )
        assert counts.messages == len(removed_ids) and counts.picture_identities_expired == len(
            targets
        )
        assert {obj.object_name for obj in store.list_objects(bucket, recursive=True)} == before
        swept = discussion_picture_cleanup.run_batch()
        assert swept.completed
        after = {obj.object_name for obj in store.list_objects(bucket, recursive=True)}
        assert not target_keys & after and live_keys <= after
        with factory() as db:
            rows = list(
                db.scalars(
                    select(m.DiscussionMessageRevision).where(
                        m.DiscussionMessageRevision.message_id.in_(removed_ids)
                    )
                )
            )
            assert rows and all(row.body is None and row.reason is None for row in rows)
        assert (
            discussion_retention.run_batch(
                factory=factory, now=security.utcnow() + timedelta(days=31)
            ).messages
            == 0
        )
        for thread, message in ids:
            url = (
                fixture["base"]
                + f"/v1/collaboration/shares/{fixture['share']}/entries/{fixture['entry']}"
                + f"/threads/{thread}/messages/{message}/moderation-history"
            )
            response = httpx.post(
                url,
                headers={"Authorization": "Bearer " + fixture["ownerToken"]},
                json={"reason": "Synthetic expired recovery check"},
                timeout=5,
            )
            assert response.status_code == 404, "Purged content remained recoverable"
    finally:
        settings.collaboration_removed_content_cleanup_enabled = old_purge
        settings.collaboration_picture_cleanup_enabled = old_sweep
    print(
        "PASS: actual SQL purge, no object I/O before sweep, private MinIO reclamation, "
        "live-picture preservation, restart idempotency and HTTP recovery refusal",
        flush=True,
    )

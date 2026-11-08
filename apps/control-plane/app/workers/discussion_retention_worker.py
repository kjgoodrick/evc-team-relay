"""Opt-in removed-content maintenance, with cooperative, fully drained shutdown."""

import asyncio
import threading
from dataclasses import asdict

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.discussion_retention import run_batch

logger = get_logger(__name__)
INITIAL_DELAY_SECONDS = 30


async def finish_batch(stop):
    job = asyncio.create_task(asyncio.to_thread(run_batch, stop))
    cancelled = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled = True
            stop.set()
        except Exception:
            break
    if cancelled:
        if not job.cancelled():
            job.exception()
        raise asyncio.CancelledError
    return job.result()


async def run_removed_content_cleanup():
    stop = threading.Event()
    try:
        await asyncio.sleep(INITIAL_DELAY_SECONDS)
        while True:
            settings = get_settings()
            if settings.collaboration_removed_content_cleanup_enabled:
                try:
                    counts = await finish_batch(stop)
                    if (
                        counts.messages
                        or counts.threads
                        or counts.picture_identities_expired
                        or counts.busy
                    ):
                        logger.info("discussion_retention", extra=asdict(counts))
                except Exception as exc:
                    logger.warning(
                        "discussion_retention_failed", extra={"reason": type(exc).__name__}
                    )
            await asyncio.sleep(settings.collaboration_removed_content_cleanup_interval_seconds)
    finally:
        stop.set()

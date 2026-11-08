"""Opt-in private-picture maintenance, with cooperative, fully drained shutdown."""

import asyncio
import threading
from dataclasses import asdict

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.discussion_picture_cleanup import run_batch
from app.workers.drain import run_drained

logger = get_logger(__name__)
INITIAL_DELAY_SECONDS = 30


async def finish_batch(stop):
    return await run_drained(lambda: run_batch(stop), stop)


async def run_picture_cleanup():
    stop = threading.Event()
    try:
        await asyncio.sleep(INITIAL_DELAY_SECONDS)
        while True:
            settings = get_settings()
            if settings.collaboration_picture_cleanup_enabled:
                try:
                    counts = await finish_batch(stop)
                    if counts.claimed:
                        logger.info("discussion_picture_cleanup", extra=asdict(counts))
                except Exception as exc:
                    logger.warning(
                        "discussion_picture_cleanup_failed", extra={"reason": type(exc).__name__}
                    )
            await asyncio.sleep(settings.collaboration_picture_cleanup_interval_seconds)
    finally:
        stop.set()

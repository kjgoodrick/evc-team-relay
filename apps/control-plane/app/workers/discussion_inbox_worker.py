"""Small SQL-backed inbox projector; no broker or platform push dependency."""

import asyncio
import threading

from app.core.logging import get_logger
from app.services.discussion_inbox import enabled, run_batch
from app.workers.drain import run_drained

logger = get_logger(__name__)


async def finish_batch(stop):
    return await run_drained(lambda: run_batch(stop), stop)


async def run_inbox_projection():
    stop = threading.Event()
    try:
        while True:
            await asyncio.sleep(2)
            if enabled():
                try:
                    await finish_batch(stop)
                except Exception as exc:
                    # No exception payloads or SQL-bound message content.
                    logger.warning(
                        "discussion_inbox_projection_failed", extra={"reason": type(exc).__name__}
                    )
    finally:
        stop.set()

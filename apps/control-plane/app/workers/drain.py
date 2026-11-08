"""Wait for admitted thread work across cooperative shutdown cancellation."""

import asyncio
import threading
from collections.abc import Callable
from typing import TypeVar, cast

from app.core.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


async def run_drained(work: Callable[[], T], stop: threading.Event | None = None) -> T:
    # Keep failures as data until the owning coroutine consumes them. Otherwise
    # a cancelled shield can log the thread exception (including SQL parameters)
    # even when a later waiter retrieves it. Only the safe exception type is
    # logged for work that fails while its owner is being cancelled.
    def invoke() -> tuple[bool, object]:
        try:
            return True, work()
        except Exception as exc:
            return False, exc

    job = asyncio.create_task(asyncio.to_thread(invoke))
    cancelled = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled = True
            if stop is not None:
                stop.set()
    succeeded, result = job.result()
    if cancelled:
        if not succeeded:
            logger.warning("worker_drain_failed", extra={"reason": type(result).__name__})
        raise asyncio.CancelledError
    if not succeeded:
        raise cast(Exception, result)
    return cast(T, result)

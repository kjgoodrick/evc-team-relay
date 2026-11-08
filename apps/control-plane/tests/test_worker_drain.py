import asyncio
import threading

import pytest

from app.workers import (
    discussion_inbox_worker,
    discussion_picture_worker,
    discussion_retention_worker,
    drain,
)


def test_drained_work_preserves_results_and_reports_ordinary_failures():
    marker = object()
    failure = RuntimeError("ordinary fixture failure")

    def fail():
        raise failure

    async def exercise():
        assert await drain.run_drained(lambda: marker) is marker
        with pytest.raises(RuntimeError) as raised:
            await drain.run_drained(fail)
        assert raised.value is failure

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "worker", [discussion_inbox_worker, discussion_picture_worker, discussion_retention_worker]
)
def test_cancelled_worker_drains_and_sanitizes_late_failure(worker, monkeypatch, caplog):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    sensitive = "synthetic private body that must not enter loop diagnostics"

    def fail(stop):
        started.set()
        assert release.wait(5)
        assert stop.is_set()
        finished.set()
        raise RuntimeError(sensitive)

    monkeypatch.setattr(worker, "run_batch", fail)

    async def exercise():
        errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: errors.append(context)
        )
        stop = threading.Event()
        task = asyncio.create_task(worker.finish_batch(stop))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert stop.is_set() and not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert finished.is_set() and errors == []

    asyncio.run(exercise())
    assert sensitive not in caplog.text
    failures = [record for record in caplog.records if record.message == "worker_drain_failed"]
    assert len(failures) == 1
    assert failures[0].reason == "RuntimeError"
    assert failures[0].exc_info is None

import asyncio
from unittest.mock import Mock

import pytest

from app import main


def run_shutdown(app):
    app.router.on_startup.clear()

    async def lifespan():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(lifespan())


def test_completion_marker_runs_after_the_registered_shutdown_hooks(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(main, "get_logger", lambda _name: logger)
    app = main.build_app()
    logger.reset_mock()
    events = []

    async def admitted_work():
        await asyncio.sleep(0)
        assert not logger.info.called
        events.append("drained")

    app.router.on_shutdown.insert(-1, admitted_work)
    run_shutdown(app)
    assert events == ["drained"]
    logger.info.assert_called_once_with("control_plane_shutdown_complete")


def test_a_failed_shutdown_hook_cannot_emit_completion(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(main, "get_logger", lambda _name: logger)
    app = main.build_app()
    logger.reset_mock()

    async def failed_work():
        raise RuntimeError("Synthetic shutdown failure")

    app.router.on_shutdown.insert(-1, failed_work)
    with pytest.raises(RuntimeError, match="Synthetic"):
        run_shutdown(app)
    logger.info.assert_not_called()

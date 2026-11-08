"""Real logging configuration must preserve backup evidence, not socket URLs."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
@pytest.mark.parametrize("format", ["json", "text"])
def test_lifecycle_evidence_survives_config_without_request_logging(level, format):
    program = """
import logging
import sys
from app.core.logging import configure_logging
configure_logging(sys.argv[1], sys.argv[2])
server = logging.getLogger("uvicorn.error")
server.info("Started server process [%d]", 123)
server.info("Application startup complete.")
server.info('%s - "WebSocket %s" [accepted]', "127.0.0.1", "/socket?token=PRIVATE")
server.info("connection open")
server.info({"private": "structured socket message"})
server.warning("Application shutdown failed.")
logging.getLogger("app.lifecycle").info("control_plane_shutdown_complete")
server.info("Application shutdown complete.")
server.info("Finished server process [%d]", 123)
configure_logging(sys.argv[1], sys.argv[2])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, level, format], capture_output=True, check=True, timeout=10
    )
    assert not result.stderr
    output = result.stdout.decode()
    for expected in (
        "Started server process [123]",
        "Application startup complete.",
        "Application shutdown failed.",
        "control_plane_shutdown_complete",
        "Application shutdown complete.",
        "Finished server process [123]",
    ):
        assert output.count(expected) == 1
    for private in ("PRIVATE", "WebSocket", "connection open", "structured socket message"):
        assert private not in output

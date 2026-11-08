#!/usr/bin/env python3
"""Exercise an explicitly supplied relay binary using disposable loopback stores.

Run with the control-plane virtualenv (provides websockets):
  apps/control-plane/.venv/bin/python scripts/test_relay_shutdown.py --binary ...
No existing service, configuration, account or store is used.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from websockets.sync.client import connect


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def get(base, path):
    with urlopen(base + path, timeout=2) as response:
        return response.read()


def start(binary, root, store, label):
    http_port = port()
    # Supply a local empty config and a clean environment; never discover a
    # user's relay.toml, webhook settings, keys or store credentials.
    config = root / "relay.toml"
    config.write_text("")
    log_path = root / f"{label}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                str(binary),
                "serve",
                str(store),
                "--config",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                str(http_port),
                "--metrics-port",
                "0",
                "--checkpoint-freq-seconds",
                "3600",
            ],
            cwd=root,
            env={"PATH": os.defpath, "RUST_LOG": "info"},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{http_port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(log_path.read_text())
            try:
                get(base, "/ready")
                return process, base, log_path
            except OSError:
                time.sleep(0.02)
        raise AssertionError("Disposable relay did not become ready")
    except BaseException:
        cleanup(process)
        raise


def cleanup(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def stop(process):
    process.send_signal(signal.SIGTERM)
    return process.wait(timeout=10)


def run(binary, root, initial, update, fail):
    label = "failed-checkpoint" if fail else "successful-checkpoint"
    folder = root / label
    folder.mkdir()
    store = folder / "store"
    doc = "disposable-shutdown-document"
    subprocess.run(
        [str(binary), "convert-from-update", str(store), doc],
        input=initial,
        cwd=folder,
        env={"PATH": os.defpath},
        capture_output=True,
        check=True,
        timeout=10,
    )
    process, base, log = start(binary, folder, store, "first")
    ws = None
    try:
        empty = get(base, f"/d/{doc}/as-update")
        checkpoint = store / doc / "data.ysweet"
        original = checkpoint.read_bytes()
        if fail:
            # Preserve the old checkpoint while making every new atomic write
            # fail. No chmod/root assumptions and no live paths are involved.
            checkpoint.parent.rename(store / "preserved-checkpoint")
            checkpoint.parent.write_text("Disposable write obstruction")
        ws = connect(
            base.replace("http:", "ws:") + f"/doc/ws/{doc}",
            open_timeout=3,
            close_timeout=1,
            proxy=None,
        )
        # Yjs sync/update framing; the committed fixture is shorter than 128B.
        assert len(update) < 128
        ws.send(bytes([0, 2, len(update)]) + update)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            applied = get(base, f"/d/{doc}/as-update")
            if applied != empty:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("WebSocket update was not applied")
        assert b"alpha beta gamma" in applied
        # Keep the upgraded socket open across SIGTERM: graceful shutdown must
        # own it and finish its final save before returning a success status.
        code = stop(process)
        output = log.read_text()
        if fail:
            assert code != 0, output
            assert "Final document checkpoint failed" in output, output
            assert "Server shut down." not in output, output
            assert (
                store / "preserved-checkpoint" / "data.ysweet"
            ).read_bytes() == original
        else:
            assert code == 0, output
            assert (
                "All document writers stopped and final checkpoints completed" in output
            )
            assert "Server shut down." in output
            assert checkpoint.read_bytes() != original
        ws.close()
        ws = None
        if not fail:
            restored, restored_base, restored_log = start(
                binary, folder, store, "reload"
            )
            try:
                assert get(restored_base, f"/d/{doc}/as-update") == applied
                assert stop(restored) == 0, restored_log.read_text()
            finally:
                cleanup(restored)
        return {
            "case": label,
            "exit_status": code,
            "open_websocket_at_signal": True,
            "restart_roundtrip": not fail,
        }
    finally:
        try:
            if ws is not None:
                ws.close()
        finally:
            cleanup(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    fixture = (
        Path(__file__).resolve().parents[1] / "apps/relay-server/crates/"
        "y-sweet-core/tests/fixtures/yjs-text-anchors-v1.json"
    )
    vectors = json.loads(fixture.read_text())["fixtures"]
    initial = bytes(next(v["update"] for v in vectors if v["name"] == "original"))
    update = bytes(next(v["update"] for v in vectors if v["name"] == "prefix"))
    with tempfile.TemporaryDirectory(prefix="relay-shutdown-proof-") as temp:
        results = [
            run(binary, Path(temp), initial, update, fail) for fail in (False, True)
        ]
        print(json.dumps({"passed": True, "results": results}, indent=2))


if __name__ == "__main__":
    main()

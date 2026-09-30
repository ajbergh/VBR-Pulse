"""Run VBR Pulse (mock mode) in a background thread for browser tests and screenshots."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from pulse.config import Settings
from pulse.web.app import create_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def running_app(scenario: str = "happy", poll_seconds: int = 1) -> Iterator[str]:
    """Yield the base URL of a live mock-mode server; stop it afterwards."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings._env_path = None  # never read the developer's real .env (lab names, users)
    settings.mock = True
    settings.mock_scenario = scenario
    settings.poll_seconds = poll_seconds
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("VBR Pulse didn't start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def browser_channel() -> str | None:
    """Use an installed Edge on Windows (no browser download); Playwright's Chromium elsewhere."""
    return os.environ.get("PULSE_E2E_CHANNEL") or ("msedge" if sys.platform == "win32" else None)

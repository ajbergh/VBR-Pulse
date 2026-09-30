"""PLAN §9 security checklist, verified."""

from __future__ import annotations

import re
import ssl
from pathlib import Path

import pytest

from pulse import cli
from pulse.config import Settings
from pulse.vbr.client import _tls_verify
from pulse.vbr.operations import OPERATIONS

SRC = Path(__file__).resolve().parents[2] / "src" / "pulse"

# The only non-GET operations v1 may call: auth, triggers, and lab-only event seeding.
ALLOWED_WRITES = {
    "CreateToken",
    "Logout",
    "StartJob",
    "StopJob",
    "RetryJob",
    "StartHyperVQuickBackupJob",
    "StartVSphereQuickBackupJob",
    "StartAgentQuickBackupJob",
    "StartMalwareBackupScan",
    "CreateSuspiciousActivityEvent",
}


def test_tls_verification_is_on_by_default() -> None:
    context = _tls_verify(True)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert _tls_verify(False) is False
    assert Settings(_env_file=None).tls_verify is True  # type: ignore[call-arg]


def test_v1_is_read_and_trigger_only() -> None:
    """Every operationId the app code calls is a GET or an allowed trigger (PLAN §2, §9)."""
    called: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "mock" in path.parts or path.name == "operations.py":
            continue
        text = path.read_text(encoding="utf-8")
        called |= set(re.findall(r'request\(\s*"([A-Za-z]+)"', text))
        called |= set(re.findall(r'"(Start[A-Za-z]+QuickBackupJob)"', text))
    assert called, "found no API calls to check"
    writes = {op for op in called if OPERATIONS[op].method != "GET"}
    assert writes <= ALLOWED_WRITES, writes - ALLOWED_WRITES
    assert not any(OPERATIONS[op].method in ("PUT", "PATCH", "DELETE") for op in called)


def test_nothing_references_port_9419() -> None:
    for path in SRC.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".html", ".js", ".css"):
            assert "9419" not in path.read_text(encoding="utf-8"), path


def test_serve_refuses_remote_binding_without_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli._serve(Settings(_env_file=None), "0.0.0.0", 8000, allow_remote=False)  # type: ignore[call-arg]  # noqa: S104
    assert code == 2
    assert "--allow-remote" in capsys.readouterr().err


def test_default_bind_is_loopback() -> None:
    assert Settings(_env_file=None).host == "127.0.0.1"  # type: ignore[call-arg]

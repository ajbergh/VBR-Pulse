"""Lab smoke tests against a real VBR 13.1 server (PLAN §11). Opt-in: `uv run pytest -m lab`.

Needs a configured .env (PULSE_VBR_URL, profiles) and the profile secrets in the keyring.
Only touches the job whose description contains `[pulse-demo]`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pulse.config import ProfileError, Settings
from pulse.inspector.bus import InspectorBus
from pulse.preflight import DEMO_TAG
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrForbidden
from pulse.vbr.sessions import SessionTracker

pytestmark = pytest.mark.lab


def _settings() -> Settings:
    settings = Settings()
    try:
        settings.profile("ops")
    except ProfileError:
        pytest.skip("No lab configured: set PULSE_VBR_URL and PULSE_PROFILE_OPS_USER in .env")
    return settings


async def _client(profile: str) -> AsyncIterator[VbrClient]:
    settings = _settings()
    client = VbrClient(
        settings.vbr_url,
        settings.profile(profile).credentials,
        verify=settings.tls_verify,
        inspector=InspectorBus(),
    )
    yield client
    await client.aclose()


@pytest.fixture
async def ops() -> AsyncIterator[VbrClient]:
    async for client in _client("ops"):
        yield client


async def test_login_and_job_states(ops: VbrClient) -> None:
    states = await ops.request("GetAllJobsStates", params={"limit": 5})
    assert "pagination" in states
    assert ops.inspector is not None
    assert all(e.request_headers.get("x-api-version") == "1.3-rev2" for e in ops.inspector.recent())


async def test_demo_job_runs_to_a_result(ops: VbrClient) -> None:
    demo = None
    async for job in ops.paginate("GetAllJobsStates"):
        if DEMO_TAG in (job.get("description") or ""):
            demo = job
            break
    if demo is None:
        pytest.skip(f"No job's description contains {DEMO_TAG}")
    session = await ops.request(
        "StartJob", path_params={"id": demo["id"]}, json={"performActiveFull": False}
    )
    tracker = SessionTracker(ops, poll_seconds=5)
    tracker.track(session)
    async with tracker.subscribe(session["id"]) as queue:
        while not (snapshot := await queue.get()).ended:
            pass
    assert snapshot.state == "Stopped"
    assert snapshot.result in ("Success", "Warning", "Failed")


async def test_viewer_is_forbidden_to_start(ops: VbrClient) -> None:
    settings = _settings()
    try:
        settings.profile("view")
    except ProfileError:
        pytest.skip("No viewer profile configured")
    states = await ops.request("GetAllJobsStates", params={"limit": 1})
    async for viewer in _client("view"):
        with pytest.raises(VbrForbidden):
            await viewer.request(
                "StartJob",
                path_params={"id": states["data"][0]["id"]},
                json={"performActiveFull": False},
            )

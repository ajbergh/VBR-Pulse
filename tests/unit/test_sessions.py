"""SessionTracker: polling cadence, backoff, fan-out, failure logs, reconnects (PLAN §4.4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from pulse.mock.app import MOCK_BASE_URL, mock_transport
from pulse.mock.state import MockVbr
from pulse.vbr.client import Credentials, VbrClient
from pulse.vbr.sessions import SessionSnapshot, SessionTracker, poll_interval
from tests.conftest import FakeClock


@pytest.fixture
def mock(clock: FakeClock) -> MockVbr:
    return MockVbr("happy", clock=clock)


@pytest.fixture
async def client(mock: MockVbr, clock: FakeClock) -> AsyncIterator[VbrClient]:
    async def no_wait(seconds: float) -> None:
        return None

    c = VbrClient(
        MOCK_BASE_URL,
        lambda: Credentials("svc-pulse-ops", SecretStr("mock")),
        transport=mock_transport(mock),
        clock=clock,
        sleep=no_wait,  # the client's own GET retries don't move the clock
    )
    yield c
    await c.aclose()


def tracker_for(client: VbrClient, clock: FakeClock, poll: float = 5.0) -> SessionTracker:
    async def advance(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return SessionTracker(client, poll_seconds=poll, clock=clock, sleep=advance)


async def start(client: VbrClient, mock: MockVbr) -> dict[str, object]:
    job_id = next(j["id"] for j in mock.jobs.values() if j["name"] == "SQL Daily")
    session: dict[str, object] = await client.request(
        "StartJob", path_params={"id": job_id}, json={"performActiveFull": False}
    )
    return session


async def drain(tracker: SessionTracker, session_id: str) -> list[SessionSnapshot]:
    seen: list[SessionSnapshot] = []
    async with tracker.subscribe(session_id) as queue:
        while True:
            snapshot = await asyncio.wait_for(queue.get(), 5)
            seen.append(snapshot)
            if snapshot.ended:
                return seen


def test_backoff_schedule() -> None:
    assert poll_interval(5, 0) == 5
    assert poll_interval(5, 119) == 5
    assert poll_interval(5, 120) == 15
    assert poll_interval(5, 600) == 30
    assert poll_interval(30, 0) == 30


async def test_polls_until_stopped(client: VbrClient, mock: MockVbr, clock: FakeClock) -> None:
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    tracker.track(session)

    seen = await drain(tracker, str(session["id"]))

    final = seen[-1]
    assert (final.state, final.result, final.progress) == ("Stopped", "Success", 100)
    assert final.history == ("Starting", "Working", "Stopped")
    assert mock.calls["GetSession"] == 9  # t = 0, 5, … 40 s
    assert final.polls == 9
    assert tracker.active_pollers() == 0
    assert "GetSessionLogs" not in mock.calls


async def test_one_update_per_poll(client: VbrClient, mock: MockVbr, clock: FakeClock) -> None:
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    async with tracker.subscribe(str(session["id"])) as queue:
        tracker.track(session)
        updates = 0
        while True:
            snapshot = await asyncio.wait_for(queue.get(), 5)
            updates += 1
            if snapshot.ended:
                break
    # Subscribed before track(), so there's no seed snapshot: exactly one update per poll.
    assert updates == mock.calls["GetSession"]


async def test_two_watchers_share_one_poller(
    client: VbrClient, mock: MockVbr, clock: FakeClock
) -> None:
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    session_id = str(session["id"])
    tracker.track(session)
    tracker.track(session)  # a second browser tab asks for the same session

    first, second = await asyncio.gather(drain(tracker, session_id), drain(tracker, session_id))

    assert first[-1] == second[-1]
    assert mock.calls["GetSession"] == 9


async def test_failed_session_fetches_logs_once(
    client: VbrClient, mock: MockVbr, clock: FakeClock
) -> None:
    mock.set_scenario("failed")
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    tracker.track(session)

    final = (await drain(tracker, str(session["id"])))[-1]

    assert final.result == "Failed"
    assert final.progress == 63
    assert mock.calls["GetSessionLogs"] == 1
    assert any(line["status"] == "Failed" for line in final.logs)
    assert len(final.logs) <= 20


async def test_connection_lost_then_recovered(
    client: VbrClient, mock: MockVbr, clock: FakeClock
) -> None:
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    mock.inject_fault("GetSession", 503, times=3)  # beats the client's 3 GET attempts once
    tracker.track(session)

    seen = await drain(tracker, str(session["id"]))

    lost = [s for s in seen if s.error]
    assert lost
    assert lost[0].retry_in == 8
    assert seen[-1].error is None
    assert seen[-1].result == "Success"


async def test_retry_now_wakes_the_poller(
    client: VbrClient, mock: MockVbr, clock: FakeClock
) -> None:
    gate = asyncio.Event()

    async def blocked(seconds: float) -> None:
        await gate.wait()

    tracker = SessionTracker(client, poll_seconds=5, clock=clock, sleep=blocked)
    session = await start(client, mock)
    mock.inject_fault("GetSession", 0, times=3)
    tracker.track(session)
    session_id = str(session["id"])
    async with tracker.subscribe(session_id) as queue:
        while not (await asyncio.wait_for(queue.get(), 5)).error:
            pass
        calls = mock.calls["GetSession"]
        tracker.retry_now(session_id)
        await asyncio.wait_for(queue.get(), 5)
    assert mock.calls["GetSession"] > calls
    await tracker.aclose()


async def test_not_found_ends_tracking(client: VbrClient, clock: FakeClock) -> None:
    tracker = tracker_for(client, clock)
    body = {"id": "00000000-0000-0000-0000-00000000dead", "name": "Gone", "state": "Working"}
    tracker.track(body)

    final = (await drain(tracker, body["id"]))[-1]

    assert final.ended
    assert final.error is not None


async def test_finish_hook_runs_once(client: VbrClient, mock: MockVbr, clock: FakeClock) -> None:
    finished: list[str] = []

    async def hook(snapshot: SessionSnapshot) -> None:
        finished.append(snapshot.result)

    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    tracker.track(session, on_finish=hook)
    tracker.track(session, on_finish=hook)

    await drain(tracker, str(session["id"]))

    assert finished == ["Success"]


async def test_running_is_keyed_by_job(client: VbrClient, mock: MockVbr, clock: FakeClock) -> None:
    tracker = tracker_for(client, clock)
    session = await start(client, mock)
    tracker.track(session)
    assert set(tracker.running()) == {session["jobId"]}
    await drain(tracker, str(session["id"]))
    assert tracker.running() == {}

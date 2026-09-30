"""Run the six mock scenarios end to end through the real VbrClient (PLAN Phase 2).

Used by `pulse scenarios` and by tests/integration/test_scenarios.py. Each run gets a
fresh MockVbr, signs in with the account the demo script uses, and checks the outcome.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from pulse.inspector.bus import InspectorBus, InspectorEvent
from pulse.mock.app import MOCK_BASE_URL, mock_transport
from pulse.mock.scenarios import SCENARIO_KEYS
from pulse.mock.state import MockVbr
from pulse.vbr.client import Credentials, VbrClient
from pulse.vbr.errors import VbrError, VbrForbidden
from pulse.vbr.incident import backup_scan_request, quick_backup_request

DEMO_JOB = "SQL Daily"
MAX_POLLS = 200


class ScenarioFailed(AssertionError):
    pass


@dataclass
class ScenarioReport:
    key: str
    title: str
    passed: bool = False
    steps: list[str] = field(default_factory=list)
    events: list[InspectorEvent] = field(default_factory=list)
    error: str | None = None


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailed(message)


class _Run:
    def __init__(
        self,
        mock: MockVbr,
        client: VbrClient,
        report: ScenarioReport,
        sleep: Callable[[float], Awaitable[None]],
        poll_seconds: float,
    ) -> None:
        self.mock = mock
        self.client = client
        self.report = report
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    def step(self, text: str) -> None:
        self.report.steps.append(text)

    async def track(self, session: dict[str, Any]) -> dict[str, Any]:
        """Poll GetSession until the session stops (the plan's session pattern)."""
        session_id = session["id"]
        for _ in range(MAX_POLLS):
            session = await self.client.request(
                "GetSession", path_params={"id": session_id}, group=f"session:{session_id}"
            )
            if session["state"] == "Stopped":
                break
            await self.sleep(self.poll_seconds)
        _check(session["state"] == "Stopped", f"session {session_id} never stopped")
        result = session["result"]["result"]
        self.step(f"Session {session['name']} stopped at {session['progressPercent']} %: {result}")
        return session

    async def demo_job(self) -> dict[str, Any]:
        states = await self.client.request(
            "GetAllJobsStates", params={"nameFilter": "SQL*", "skip": 0, "limit": 25}
        )
        self.step(f"Job filter 'SQL*' matched {states['pagination']['total']} jobs")
        job = next((j for j in states["data"] if j["name"] == DEMO_JOB), None)
        _check(job is not None, f"{DEMO_JOB} not found")
        return dict(job or {})

    async def start_demo_job(self) -> dict[str, Any]:
        job = await self.demo_job()
        session = await self.client.request(
            "StartJob", path_params={"id": job["id"]}, json={"performActiveFull": False}
        )
        self.step(f"Started {job['name']}: session {session['id'][:8]} is {session['state']}")
        return dict(session)

    # ------------------------------------------------------------------ scenarios

    async def result_scenario(self, expected: str) -> None:
        info = await self.client.request("GetServerInfo")
        self.step(f"Connected to {info['name']} · {info['buildVersion']}")
        session = await self.track(await self.start_demo_job())
        _check(session["result"]["result"] == expected, f"expected {expected}")
        if expected == "Failed":
            logs = await self.client.request("GetSessionLogs", path_params={"id": session["id"]})
            failed = [r["title"] for r in logs["records"] if r["status"] == "Failed"]
            _check(bool(failed), "no Failed log lines")
            self.step(f"Fetched logs: {failed[0]}")

    async def token_expiry(self) -> None:
        await self.track(await self.start_demo_job())
        events = self.client.inspector.recent() if self.client.inspector else []
        expired = [e for e in events if e.status == 401]
        refreshes = [
            e
            for e in events
            if e.operation_id == "CreateToken"
            and isinstance(e.request_body, dict)
            and e.request_body.get("grant_type") == "refresh_token"
        ]
        _check(bool(expired), "the access token never expired")
        _check(bool(refreshes), "no refresh_token grant")
        self.step(
            f"Access token expired ({expired[0].error_code}); refreshed silently "
            f"{len(refreshes)} time(s) and retried"
        )

    async def forbidden(self) -> None:
        job = await self.demo_job()
        try:
            await self.client.request(
                "StartJob", path_params={"id": job["id"]}, json={"performActiveFull": False}
            )
        except VbrForbidden as exc:
            role = self.mock.role_of("svc-pulse-view")
            self.step(f"StartJob returned 403 {exc.error_code} for the {role} role")
            return
        raise ScenarioFailed("StartJob wasn't forbidden")

    async def incident(self) -> None:
        events = await self.client.request(
            "ViewSuspiciousActivityEvents",
            params={"orderColumn": "DetectionTimeUtc", "orderAsc": False},
        )
        _check(events["pagination"]["total"] >= 1, "no malware events")
        event = events["data"][0]
        machine = event["machine"]
        self.step(f"Malware event: {event['details']}")

        backup_object = await self.client.request(
            "GetBackupObject", path_params={"id": machine["backupObjectId"]}
        )
        operation_id, body = quick_backup_request(backup_object)
        quick = await self.client.request(operation_id, json=body)
        self.step(f"Quick backup of {backup_object['name']} started ({operation_id})")
        quick = await self.track(quick)
        _check(quick["result"]["result"] == "Success", "quick backup failed")

        points = await self.client.request(
            "GetAllObjectRestorePoints",
            params={
                "backupObjectIdFilter": machine["backupObjectId"],
                "orderColumn": "CreationTime",
                "orderAsc": False,
                "limit": 1,
            },
        )
        latest = points["data"][0]
        _check(latest["sessionId"] == quick["id"], "quick backup didn't create a restore point")
        self.step(f"New restore point {latest['id'][:8]} from the quick backup")

        scan = await self.client.request(
            "StartMalwareBackupScan",
            json=backup_scan_request(backup_object["backupId"], machine["backupObjectId"]),
        )
        self.step("Backup scan started")
        scan = await self.track(scan)
        _check(scan["result"]["result"] == "Success", "scan failed")


ACCOUNT = {"incident": "svc-pulse-ir", "forbidden": "svc-pulse-view"}


async def run_scenario(
    key: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    speed: float = 1.0,
    poll_seconds: float = 5.0,
    transport_wrapper: Callable[[Any], Any] | None = None,
) -> ScenarioReport:
    """Run one scenario. `poll_seconds` is in scenario time; real waits are divided by `speed`."""
    mock = MockVbr(key, clock=clock, speed=speed)
    report = ScenarioReport(key=key, title=mock.scenario.title)
    bus = InspectorBus(capacity=1000)
    transport = mock_transport(mock)
    if transport_wrapper is not None:
        transport = transport_wrapper(transport)
    username = ACCOUNT.get(key, "svc-pulse-ops")
    client = VbrClient(
        MOCK_BASE_URL,
        lambda: Credentials(username, SecretStr("mock")),
        inspector=bus,
        mode="mock",
        transport=transport,
        clock=clock,
        sleep=sleep,
    )
    run = _Run(mock, client, report, sleep, poll_seconds / speed)
    run.step(f"Account {username} ({mock.role_of(username)})")
    try:
        match key:
            case "happy":
                await run.result_scenario("Success")
            case "warning":
                await run.result_scenario("Warning")
            case "failed":
                await run.result_scenario("Failed")
            case "token-expiry":
                await run.token_expiry()
            case "forbidden":
                await run.forbidden()
            case "incident":
                await run.incident()
            case _:
                raise ScenarioFailed(f"no script for scenario {key!r}")
        report.passed = True
    except (ScenarioFailed, VbrError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        await client.aclose()
        report.events = bus.recent()
    return report


async def run_all(**kwargs: Any) -> list[ScenarioReport]:
    return [await run_scenario(key, **kwargs) for key in SCENARIO_KEYS]

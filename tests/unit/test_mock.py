"""Mock VBR behaviour: timelines, paging, RBAC, stop/retry, quick backup variants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pulse.mock.app import MOCK_BASE_URL, mock_transport
from pulse.mock.scenarios import load_scenarios
from pulse.mock.state import ACCOUNTS, MockError, MockVbr, Request
from tests.conftest import FakeClock


@pytest.fixture
def mock(clock: FakeClock) -> MockVbr:
    return MockVbr("happy", clock=clock)


def req(user: str = "svc-pulse-ops", **kwargs: Any) -> Request:
    return Request(username=user, token="t", **kwargs)


def sql_daily(mock: MockVbr) -> str:
    return next(j["id"] for j in mock.jobs.values() if j["name"] == "SQL Daily")


# ---------------------------------------------------------------- timelines


def test_progress_interpolates_between_working_steps() -> None:
    timeline = load_scenarios()["happy"].timeline("job")
    assert timeline.at(0)["state"] == "Starting"
    assert timeline.at(4)["progressPercent"] == 2
    assert timeline.at(21)["progressPercent"] == 50
    assert timeline.at(40)["result"]["result"] == "Success"
    assert timeline.at(999)["state"] == "Stopped"


def test_failed_scenario_stops_at_63() -> None:
    timeline = load_scenarios()["failed"].timeline("job")
    assert timeline.at(25)["progressPercent"] == 63
    end = timeline.at(30)
    assert (end["state"], end["progressPercent"], end["result"]["result"]) == (
        "Stopped",
        63,
        "Failed",
    )


def test_result_is_none_while_running() -> None:
    view = load_scenarios()["warning"].timeline("job").at(10)
    assert view["result"] == {"result": "None", "message": "", "isCanceled": False}


# ---------------------------------------------------------------- collections


def test_name_filter_is_wildcard_and_case_insensitive(mock: MockVbr) -> None:
    page = mock.get_all_jobs_states(req(query={"nameFilter": "sql*"}))
    assert [j["name"] for j in page["data"]] == ["SQL Daily", "SQL Logs hourly", "SQL Reporting"]


def test_type_filter_and_paging(mock: MockVbr) -> None:
    page = mock.get_all_jobs_states(req(query={"typeFilter": "HyperVBackup", "limit": "3"}))
    assert page["pagination"]["count"] == 3
    assert page["pagination"]["total"] > 3
    assert all(j["type"] == "HyperVBackup" for j in page["data"])


def test_server_side_sorting(mock: MockVbr) -> None:
    page = mock.get_all_repositories_states(
        req(query={"orderColumn": "FreeGB", "orderAsc": "false"})
    )
    free = [r["freeGB"] for r in page["data"]]
    assert free == sorted(free, reverse=True)


def test_unknown_order_column_is_400(mock: MockVbr) -> None:
    with pytest.raises(MockError) as err:
        mock.get_all_repositories_states(req(query={"orderColumn": "Nope"}))
    assert err.value.status == 400


def test_fixture_times_are_rebased_to_now(mock: MockVbr) -> None:
    job = mock.jobs[sql_daily(mock)]
    now = mock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    assert job["lastRun"] < now < job["nextRun"]


# ---------------------------------------------------------------- jobs and sessions


def test_job_state_follows_live_session(mock: MockVbr, clock: FakeClock) -> None:
    jid = sql_daily(mock)
    mock.start_job(req(path={"id": jid}))
    clock.advance(20)
    running = mock.job_state(jid)
    assert running["status"] == "Running"
    assert 0 < running["progressPercent"] < 100
    clock.advance(30)
    done = mock.job_state(jid)
    assert (done["status"], done["lastResult"], done["progressPercent"]) == (
        "Inactive",
        "Success",
        0,
    )


def test_stop_job(mock: MockVbr, clock: FakeClock) -> None:
    jid = sql_daily(mock)
    session = mock.start_job(req(path={"id": jid}))
    clock.advance(10)
    assert mock.stop_job(req(path={"id": jid}))["state"] == "Stopping"
    clock.advance(5)
    stopped = mock.get_session(req(path={"id": session["id"]}))
    assert stopped["state"] == "Stopped"
    assert stopped["result"]["isCanceled"] is True
    logs = mock.get_session_logs(req(path={"id": session["id"]}))
    assert logs["records"][-1]["title"] == "Job has been stopped by user"


def test_stop_idle_job_is_400(mock: MockVbr) -> None:
    with pytest.raises(MockError, match="isn't running"):
        mock.stop_job(req(path={"id": sql_daily(mock)}))


def test_retry_after_failure(mock: MockVbr, clock: FakeClock) -> None:
    mock.set_scenario("failed")
    jid = sql_daily(mock)
    with pytest.raises(MockError, match="retried"):
        mock.retry_job(req(path={"id": jid}))  # last result is Success in the fixture
    mock.start_job(req(path={"id": jid}))
    clock.advance(60)
    mock.set_scenario("happy")
    retry = mock.retry_job(req(path={"id": jid}))
    assert retry["state"] == "Starting"


def test_disabled_job_cannot_start(mock: MockVbr) -> None:
    jid = next(j["id"] for j in mock.jobs.values() if j["status"] == "Disabled")
    with pytest.raises(MockError, match="disabled"):
        mock.start_job(req(path={"id": jid}))


def test_logs_appear_as_the_session_progresses(mock: MockVbr, clock: FakeClock) -> None:
    session = mock.start_job(req(path={"id": sql_daily(mock)}))
    early = mock.get_session_logs(req(path={"id": session["id"]}))["totalRecords"]
    clock.advance(40)
    late = mock.get_session_logs(req(path={"id": session["id"]}))
    assert early < late["totalRecords"]
    assert "{" not in late["records"][-1]["title"]


def test_sessions_newest_first_and_include_live(mock: MockVbr, clock: FakeClock) -> None:
    clock.advance(5)
    live = mock.start_job(req(path={"id": sql_daily(mock)}))
    page = mock.get_all_sessions(req(query={"limit": "50"}))
    assert page["data"][0]["id"] == live["id"]
    times = [s["creationTime"] for s in page["data"]]
    assert times == sorted(times, reverse=True)


def test_agent_quick_backup_returns_job_id_and_session_is_findable(mock: MockVbr) -> None:
    body = {
        "platform": "Agent",
        "id": "9a7c5d1e-2b3f-4a6e-8c0d-1e2f3a4b5c6d",
        "name": "LAPTOP-042",
        "type": "WindowsComputer",
        "protectionGroupId": "1e2f3a4b-5c6d-4e7f-8a9b-0c1d2e3f4a5b",
    }
    result = mock.start_agent_quick_backup(req(body=body))
    assert set(result) == {"jobId"}
    sessions = mock.get_all_sessions(req(query={"jobIdFilter": result["jobId"]}))
    assert sessions["pagination"]["total"] >= 1


# ---------------------------------------------------------------- auth and RBAC


@pytest.mark.parametrize(
    ("user", "operation_id", "allowed"),
    [
        ("svc-pulse-ops", "StartJob", True),
        ("svc-pulse-view", "StartJob", False),
        ("svc-pulse-view", "GetAllJobsStates", True),
        ("svc-pulse-ir", "StartMalwareBackupScan", True),
        ("svc-pulse-ir", "StartHyperVQuickBackupJob", True),
        ("svc-pulse-ir", "StartJob", False),
        ("svc-pulse-ops", "CreateSuspiciousActivityEvent", False),
    ],
)
def test_role_matrix(mock: MockVbr, user: str, operation_id: str, allowed: bool) -> None:
    if allowed:
        mock.authorize(user, operation_id)
    else:
        with pytest.raises(MockError) as err:
            mock.authorize(user, operation_id)
        assert err.value.status == 403


def test_forbidden_scenario_forces_viewer_role(mock: MockVbr) -> None:
    mock.set_scenario("forbidden")
    assert all(mock.role_of(user) == "Backup Viewer" for user in ACCOUNTS)


def test_access_token_expires_at_900_s(mock: MockVbr, clock: FakeClock) -> None:
    token = mock.create_token(
        {"grant_type": "password", "username": "LAB\\svc-pulse-ops", "password": "x"}
    )
    auth = f"Bearer {token['access_token']}"
    clock.advance(899)
    assert mock.authenticate(auth)[0] == "svc-pulse-ops"
    clock.advance(1)
    with pytest.raises(MockError, match="expired"):
        mock.authenticate(auth)


def test_incident_event_only_in_incident_scenario(mock: MockVbr) -> None:
    assert mock.malware_events() == []
    mock.set_scenario("incident")
    assert len(mock.malware_events()) == 1


# ---------------------------------------------------------------- HTTP layer


@pytest.fixture
async def http(mock: MockVbr) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=mock_transport(mock), base_url=MOCK_BASE_URL) as c:
        yield c


async def test_missing_api_version_is_400(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/v1/serverTime")
    assert response.status_code == 400
    assert "x-api-version" in response.json()["message"]


async def test_missing_token_is_401(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/v1/serverTime", headers={"x-api-version": "1.3-rev2"})
    assert response.status_code == 401
    assert response.json()["errorCode"] == "InvalidToken"


async def test_unmocked_path_is_404(http: httpx.AsyncClient) -> None:
    response = await http.get("/api/v1/nope", headers={"x-api-version": "1.3-rev2"})
    assert response.status_code == 404


async def test_mock_validates_request_bodies(mock: MockVbr, http: httpx.AsyncClient) -> None:
    token = mock.create_token(
        {"grant_type": "password", "username": "svc-pulse-ir", "password": "x"}
    )
    response = await http.post(
        "/api/v1/malwareDetection/scanBackup",
        json={"type": "Backup"},
        headers={"x-api-version": "1.3-rev2", "Authorization": f"Bearer {token['access_token']}"},
    )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "UnexpectedContent"

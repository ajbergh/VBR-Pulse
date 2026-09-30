"""Web layer (PLAN Phase 3): sign-in, partials, actions, SSE, errors, security headers."""

from __future__ import annotations

import html as html_lib
import re
import threading
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from pulse.config import Settings
from pulse.mock.state import MockVbr
from pulse.vbr.client import Credentials
from pulse.web.app import create_app
from pulse.web.state import COOKIE, AppState

HX = {"HX-Request": "true"}


@pytest.fixture
def mock() -> MockVbr:
    return MockVbr("happy", speed=40)  # a 40 s timeline plays in ~1 s


@pytest.fixture
def app(mock: MockVbr) -> FastAPI:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.mock = True
    app = create_app(settings, mock=mock)
    app.state.pulse.set_poll_seconds(0.05)
    return app


@pytest.fixture
def web(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def state(app: FastAPI) -> AppState:
    pulse: AppState = app.state.pulse
    return pulse


def sign_in(web: TestClient, profile: str = "ops") -> None:
    response = web.post(
        "/signin", data={"mode": "mock", "profile": profile}, follow_redirects=False
    )
    assert response.status_code == 303


def job_id(mock: MockVbr, name: str = "SQL Daily") -> str:
    return next(j["id"] for j in mock.jobs.values() if j["name"] == name)


def urls(app: FastAPI) -> list[str]:
    return [e.url for e in state(app).bus.recent()]


# ---------------------------------------------------------------- sign in


def test_signed_out_pages_redirect(web: TestClient) -> None:
    response = web.get("/jobs", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/signin")
    htmx = web.get("/ui/jobs/table", headers=HX)
    assert htmx.headers["HX-Redirect"] == "/signin"
    assert web.get("/events/inspector").status_code == 204


def test_sign_in_sets_a_strict_http_only_cookie(web: TestClient, app: FastAPI) -> None:
    response = web.post("/signin", data={"mode": "mock", "profile": "ops"}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert response.headers["location"] == "/jobs"
    ops = [e.operation_id for e in state(app).bus.recent()]
    assert ops == ["CreateToken", "GetServerInfo"]  # PLAN §7.5.1


def test_incident_account_lands_on_incident(web: TestClient) -> None:
    response = web.post("/signin", data={"mode": "mock", "profile": "ir"}, follow_redirects=False)
    assert response.headers["location"] == "/incident"


def test_signout_logs_out(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    web.post("/signout", follow_redirects=False)
    assert mock.calls["Logout"] == 1
    assert web.get("/jobs", follow_redirects=False).status_code == 303


def test_shutdown_logs_out_every_connection(app: FastAPI, mock: MockVbr) -> None:
    with TestClient(app) as first, TestClient(app) as second:
        sign_in(first)
        sign_in(second, "view")
    assert mock.calls["Logout"] == 2


# ---------------------------------------------------------------- jobs


def test_jobs_page(web: TestClient) -> None:
    sign_in(web)
    page = web.get("/jobs").text
    assert "Showing 1–25 of 42" in page
    assert 'sse-connect="/events/inspector"' in page
    assert "Mock" in page


def test_filter_appends_wildcard(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    html = web.get("/ui/jobs/table?q=SQL", headers=HX).text
    assert html.count('class="job-row') == 3
    assert "nameFilter=SQL%2A" in urls(app)[-1]


def test_empty_filter_copy(web: TestClient) -> None:
    sign_in(web)
    html = web.get("/ui/jobs/table?q=zzz", headers=HX).text
    assert "No jobs match 'zzz*'. Clear the filter to see all 42 jobs." in html_lib.unescape(html)


def test_paging(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    html = web.get("/ui/jobs/table?page=2", headers=HX).text
    assert "Showing 26–42 of 42" in html
    assert "skip=25" in urls(app)[-1]


def test_job_panel(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    html = web.get(f"/ui/jobs/{job_id(mock)}", headers=HX).text
    assert "Recent sessions" in html
    assert "SQL Daily" in html


def test_start_returns_the_session_track(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    html = web.post(f"/ui/jobs/{job_id(mock)}/start", headers=HX).text
    assert 'role="progressbar"' in html
    assert re.search(r'sse-connect="/events/session/[0-9a-f-]{36}"', html)
    assert "Started SQL Daily. Watching the session." in html
    assert 'hx-swap-oob="true"' in html and "<template>" in html  # the job row updates too


def test_sse_delivers_an_update_per_poll(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    html = web.post(f"/ui/jobs/{job_id(mock)}/start", headers=HX).text
    session_id = re.search(r"/events/session/([0-9a-f-]{36})", html).group(1)  # type: ignore[union-attr]

    with web.stream("GET", f"/events/session/{session_id}") as stream:
        body = stream.read().decode()

    updates = body.count("event: update")
    assert "event: done" in body
    assert "Success" in body
    assert updates >= 2
    # Every poll after we subscribed produced an update (plus the snapshot we joined with).
    assert updates >= mock.calls["GetSession"] - 1


def test_two_tabs_share_one_poller(web: TestClient, app: FastAPI, mock: MockVbr) -> None:
    sign_in(web)
    html = web.post(f"/ui/jobs/{job_id(mock)}/start", headers=HX).text
    session_id = re.search(r"/events/session/([0-9a-f-]{36})", html).group(1)  # type: ignore[union-attr]
    bodies: list[str] = []

    def watch() -> None:
        with web.stream("GET", f"/events/session/{session_id}") as stream:
            bodies.append(stream.read().decode())

    tabs = [threading.Thread(target=watch) for _ in range(2)]
    for tab in tabs:
        tab.start()
    for tab in tabs:
        tab.join(timeout=30)

    assert len(bodies) == 2
    conn = next(iter(state(app).connections.values()))
    assert conn.tracker.active_pollers() == 0
    # One poller: total GetSession calls track one tab's updates, not two tabs' worth.
    assert mock.calls["GetSession"] <= max(b.count("event: update") for b in bodies) + 1


def test_viewer_gets_a_role_callout(web: TestClient, mock: MockVbr) -> None:
    sign_in(web, "view")
    response = web.post(f"/ui/jobs/{job_id(mock)}/start", headers=HX)
    assert response.headers["HX-Reswap"] == "none"
    html = html_lib.unescape(response.text)
    assert "This account's role can't do that." in html
    assert "svc-pulse-view is a Backup Viewer and can't start jobs." in html
    assert "Switch to the operator profile." in html
    assert "AccessDenied" in html


def test_not_found_toast(web: TestClient) -> None:
    sign_in(web)
    response = web.get("/ui/sessions/00000000-0000-0000-0000-000000000000/logs", headers=HX)
    assert "That object no longer exists. The list has been refreshed." in response.text
    assert response.headers["HX-Trigger"] == "pulse:refresh"


def test_server_error_toast_offers_retry(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    mock.inject_fault("GetAllJobsStates", 503, times=3)
    response = web.get("/ui/jobs/table", headers={**HX, "HX-Target": "jobs-table"})
    assert "Try again" in response.text
    assert 'hx-target="#jobs-table"' in response.text


def test_expired_session_returns_to_sign_in(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    conn = next(iter(state(app).connections.values()))
    # Revoke every token and make the silent re-login fail too.
    conn.client._credentials = lambda: Credentials("intruder", SecretStr("x"))
    state(app).mock._access.clear()
    state(app).mock._refresh.clear()
    response = web.get("/ui/jobs/table", headers=HX)
    assert response.headers["HX-Redirect"] == "/signin?expired=true"
    page = web.get("/signin?expired=true").text
    assert "Your session expired. Sign in again." in html_lib.unescape(page)


def test_switch_profile(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    response = web.post(
        "/ui/switch-profile",
        data={"profile": "view"},
        headers={**HX, "HX-Current-URL": "http://testserver/repositories"},
    )
    assert response.headers["HX-Redirect"] == "/repositories"
    conn = next(iter(state(app).connections.values()))
    assert conn.username == "svc-pulse-view"


# ---------------------------------------------------------------- other screens


def test_repositories_sort_is_server_side(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    web.get("/ui/repositories/cards?sort=free", headers=HX)
    assert "orderColumn=FreeGB&orderAsc=false" in urls(app)[-1]


def test_sessions_and_logs(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    page = web.get("/sessions").text
    assert "Showing 1–50 of" in page
    session_id = mock.history[0]["id"]
    logs = web.get(f"/ui/sessions/{session_id}/logs", headers=HX).text
    assert "Job started at" in logs


def test_incident_flow(web: TestClient, mock: MockVbr, app: FastAPI) -> None:
    mock.set_scenario("incident")
    sign_in(web, "ir")
    page = web.get("/incident").text
    event_id = re.search(r'name="event_id" value="([0-9a-f-]{36})"', page).group(1)  # type: ignore[union-attr]
    stepper = web.post("/ui/incident/event", data={"event_id": event_id}, headers=HX).text
    assert "Start quick backup" in stepper
    stepper = web.post("/ui/incident/quick-backup", headers=HX).text
    quick = re.search(r"/events/session/([0-9a-f-]{36})", stepper).group(1)  # type: ignore[union-attr]
    with web.stream("GET", f"/events/session/{quick}") as stream:
        body = stream.read().decode()
    assert "Scan latest restore point" in body  # the stepper unlocks step 3 out of band
    stepper = web.post("/ui/incident/scan", headers=HX).text
    scan = re.search(r"/events/session/([0-9a-f-]{36})", stepper.split("Scan backup")[1]).group(1)  # type: ignore[union-attr]
    with web.stream("GET", f"/events/session/{scan}") as stream:
        stream.read()
    assert "Responded to the event" in web.get("/ui/incident/stepper", headers=HX).text
    ops = [e.operation_id for e in state(app).bus.recent()]
    for op in (
        "ViewSuspiciousActivityEvents",
        "GetBackupObject",
        "StartHyperVQuickBackupJob",
        "StartMalwareBackupScan",
    ):
        assert op in ops


def test_settings_scenario_and_poll(web: TestClient, mock: MockVbr, app: FastAPI) -> None:
    sign_in(web)
    web.post("/ui/settings/scenario", data={"scenario": "warning"}, headers=HX)
    assert mock.scenario.key == "warning"
    web.post("/ui/settings/poll", data={"seconds": "10"}, headers=HX)
    assert state(app).poll_seconds == 10


def test_seed_event_needs_the_incident_role(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    denied = web.post("/ui/settings/seed-event", headers=HX)
    assert "can't create malware events" in html_lib.unescape(denied.text)
    web.post("/ui/switch-profile", data={"profile": "ir"}, headers=HX)
    ok = web.post("/ui/settings/seed-event", headers=HX)
    assert "Seeded a malware event" in ok.text
    assert len(mock.malware_events()) == 1


def test_inspector_detail_and_token_ring(web: TestClient, app: FastAPI) -> None:
    sign_in(web)
    event = state(app).bus.recent()[0]
    detail = web.get(f"/ui/inspector/{event.id}", headers=HX).text
    assert "Copy as cURL" in detail
    assert "$VBR_PASSWORD" in detail
    assert "operationId: <code>CreateToken</code>" in detail
    assert "data-expires-at" in web.get("/ui/token", headers=HX).text


# ---------------------------------------------------------------- security (PLAN §9)


def test_security_headers(web: TestClient) -> None:
    headers = web.get("/signin").headers
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_cross_origin_post_refused(web: TestClient) -> None:
    response = web.post(
        "/signin",
        data={"mode": "mock", "profile": "ops"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_no_token_or_password_reaches_the_browser(
    web: TestClient, app: FastAPI, mock: MockVbr
) -> None:
    sign_in(web)
    pages = [web.get(p).text for p in ("/jobs", "/repositories", "/sessions", "/settings")]
    pages += [web.get(f"/ui/inspector/{e.id}", headers=HX).text for e in state(app).bus.recent()]
    secrets = [*mock._access, *mock._refresh]
    assert secrets
    for html in pages:
        for secret in secrets:
            assert secret not in html


def test_insecure_tls_banner(mock: MockVbr) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.lab_insecure_tls = True
    with TestClient(create_app(settings, mock=mock)) as client:
        assert "Certificate checks are off. Lab use only." in client.get("/signin").text


# ---------------------------------------------------------------- phase 5–7 acceptance


@pytest.mark.parametrize(
    ("scenario", "chip", "logs"),
    [("happy", "chip-ok", False), ("warning", "chip-warn", False), ("failed", "chip-bad", True)],
)
def test_track_result_chip_and_logs(
    web: TestClient, mock: MockVbr, scenario: str, chip: str, logs: bool
) -> None:
    mock.set_scenario(scenario)
    sign_in(web)
    html = web.post(f"/ui/jobs/{job_id(mock)}/start", headers=HX).text
    session_id = re.search(r"/events/session/([0-9a-f-]{36})", html).group(1)  # type: ignore[union-attr]
    with web.stream("GET", f"/events/session/{session_id}") as stream:
        final = stream.read().decode().split("event: update")[-1]
    assert chip in final
    assert ("log-lines" in final) is logs
    if scenario == "failed":
        assert "SQL Daily failed at 63 %. The last log lines are below." in html_lib.unescape(
            final.replace("data: ", "")
        )
        assert mock.calls["GetSessionLogs"] == 1
    if scenario == "warning":
        assert "1 of 4 machines was skipped" in final


def test_capacity_bars_match_the_api(web: TestClient, mock: MockVbr) -> None:
    sign_in(web)
    html = web.get("/ui/repositories/cards", headers=HX).text
    for repo in mock.repositories:
        used = 100 * repo["usedSpaceGB"] / repo["capacityGB"]
        assert f"width: {round(used, 1)}%" in html, repo["name"]


def test_incident_steps_stay_locked(web: TestClient, mock: MockVbr) -> None:
    mock.set_scenario("incident")
    sign_in(web, "ir")
    web.post("/ui/incident/scan", headers=HX)  # step 3 before steps 1 and 2
    web.post("/ui/incident/quick-backup", headers=HX)  # step 2 before step 1
    assert mock.calls["StartMalwareBackupScan"] == 0
    assert mock.calls["StartHyperVQuickBackupJob"] == 0

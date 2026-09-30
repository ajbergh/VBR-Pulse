"""Pages, htmx partials and actions (PLAN §7.5). Every VBR call goes through conn.client."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from pulse.config import ProfileError
from pulse.mock.scenarios import SCENARIO_KEYS
from pulse.vbr.errors import VbrError
from pulse.vbr.incident import UnsupportedMachine, backup_scan_request, quick_backup_request
from pulse.vbr.sessions import SessionSnapshot
from pulse.web.render import (
    Callout,
    NotSignedIn,
    callout_for,
    connection,
    hx_redirect,
    is_htmx,
    pulse_state,
    render,
    render_partial,
    set_session_cookie,
    toast_html,
)
from pulse.web.state import COOKIE, AppState, Connection, SignInError
from pulse.web.views import JOB_TYPES

router = APIRouter()

JOBS_PER_PAGE = 25
SESSIONS_PER_PAGE = 50
POLL_CHOICES = (5, 10, 30)
REPO_SORTS = {
    "name": ("Name", True, "Name"),
    "free": ("FreeGB", False, "Most free space"),
    "used": ("UsedSpaceGB", False, "Most used space"),
    "capacity": ("CapacityGB", False, "Largest capacity"),
}

Conn = Annotated[Connection, Depends(connection)]
State = Annotated[AppState, Depends(pulse_state)]


async def attempt(
    conn: Connection, state: AppState, call: Callable[[], Awaitable[Any]]
) -> tuple[Any, Callout | None]:
    """Run a read for a page; turn a VBR error into an on-page callout instead of a crash."""
    try:
        return await call(), None
    except VbrError as exc:
        if exc.status == 401:
            raise
        return None, callout_for(exc, conn, state)


# ---------------------------------------------------------------- sign in / out


@router.get("/", include_in_schema=False)
async def home(request: Request, state: State) -> Response:
    target = "/jobs" if state.get(request.cookies.get(COOKIE)) else "/signin"
    return RedirectResponse(target, status_code=303)


@router.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request, state: State, expired: bool = False) -> Response:
    notice = "Your session expired. Sign in again." if expired else None
    return _signin(request, state, notice=notice)


def _signin(
    request: Request,
    state: AppState,
    *,
    error: str | None = None,
    notice: str | None = None,
    mode: str | None = None,
    status_code: int = 200,
) -> Response:
    try:
        live_profiles = state.profiles("live")
        profile_error = None
    except ProfileError as exc:
        live_profiles, profile_error = [], str(exc)
    # Nothing configured yet (e.g. a fresh release download): offer mock data first.
    mode = mode or ("mock" if state.settings.mock or not live_profiles else "live")
    return render(
        request,
        "signin.html",
        status_code=status_code,
        mode=mode,
        live_profiles=live_profiles,
        mock_profiles=state.profiles("mock"),
        profile_error=profile_error,
        server_url=state.settings.vbr_url,
        error=error,
        notice=notice,
    )


@router.post("/signin")
async def signin(
    request: Request,
    state: State,
    profile: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "live",
    server: Annotated[str, Form()] = "",
) -> Response:
    if mode not in ("live", "mock"):
        mode = "live"
    await state.disconnect(request.cookies.get(COOKIE))
    try:
        conn = await state.connect(mode, profile, server or None)  # type: ignore[arg-type]
    except SignInError as exc:
        return _signin(request, state, error=str(exc), mode=mode, status_code=400)
    # The incident account can't read jobs, so it starts where it can act (PLAN §7.5.5).
    landing = "/incident" if conn.profile.name == "ir" else "/jobs"
    response = RedirectResponse(landing, status_code=303)
    set_session_cookie(response, conn.id)
    return response


@router.post("/signout")
async def signout(request: Request, state: State) -> Response:
    await state.disconnect(request.cookies.get(COOKIE))
    response = RedirectResponse("/signin", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.post("/ui/switch-profile")
async def switch_profile(
    request: Request, state: State, conn: Conn, profile: Annotated[str, Form()]
) -> Response:
    """The RBAC demo: same server, different account (PLAN §12 step 6)."""
    mode, server = conn.mode, conn.server_url
    back = urlsplit(request.headers.get("HX-Current-URL", "")).path or "/jobs"
    await state.disconnect(conn.id)
    try:
        new = await state.connect(mode, profile, server)
    except SignInError as exc:
        response = _signin(request, state, error=str(exc), mode=mode)
        response.delete_cookie(COOKIE)
        return hx_redirect(request, "/signin") if is_htmx(request) else response
    response = hx_redirect(request, back if back.startswith("/") else "/jobs")
    set_session_cookie(response, new.id)
    return response


# ---------------------------------------------------------------- jobs


async def _job_page(conn: Connection, q: str, job_type: str, page: int) -> dict[str, Any]:
    name_filter = f"{q.strip()}*" if q.strip() and not q.strip().endswith("*") else q.strip()
    params: dict[str, Any] = {
        "skip": (page - 1) * JOBS_PER_PAGE,
        "limit": JOBS_PER_PAGE,
        "orderColumn": "Name",
        "orderAsc": True,
        "nameFilter": name_filter or None,
        "typeFilter": job_type or None,
    }
    result = await conn.client.request("GetAllJobsStates", params=params)
    for job in result["data"]:
        conn.job_cache[job["id"]] = job
    total = result["pagination"]["total"]
    return {
        "jobs": result["data"],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / JOBS_PER_PAGE)),
        "first": (page - 1) * JOBS_PER_PAGE + 1 if total else 0,
        "last": min(page * JOBS_PER_PAGE, total),
        "q": q,
        "name_filter": name_filter,
        "job_type": job_type,
    }


async def _jobs_total(conn: Connection) -> int | None:
    try:
        result = await conn.client.request("GetAllJobsStates", params={"limit": 1})
    except VbrError:
        return None
    return int(result["pagination"]["total"])


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request, state: State, conn: Conn, q: str = "", type: str = "", page: int = 1
) -> Response:
    data, problem = await attempt(conn, state, lambda: _job_page(conn, q, type, max(page, 1)))
    dock = conn.tracker.latest(conn.dock_session) if conn.dock_session else None
    return render(
        request,
        "jobs.html",
        conn=conn,
        active="jobs",
        table=data,
        problem=problem,
        job_types=JOB_TYPES,
        dock=dock,
        live=conn.tracker.running(),
    )


@router.get("/ui/jobs/table", response_class=HTMLResponse)
async def jobs_table(
    request: Request, state: State, conn: Conn, q: str = "", type: str = "", page: int = 1
) -> Response:
    data = await _job_page(conn, q, type, max(page, 1))
    all_total = await _jobs_total(conn) if data["total"] == 0 and (q or type) else None
    return render_partial(
        request,
        "partials/jobs_table.html",
        conn=conn,
        table=data,
        all_total=all_total,
        live=conn.tracker.running(),
    )


@router.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
async def job_panel(request: Request, state: State, conn: Conn, job_id: str) -> Response:
    states = await conn.client.request("GetAllJobsStates", params={"idFilter": job_id})
    if not states["data"]:
        return render_partial(request, "partials/job_panel.html", conn=conn, job=None, sessions=[])
    job = conn.job_cache[job_id] = states["data"][0]
    sessions, problem = await attempt(
        conn,
        state,
        lambda: conn.client.request(
            "GetAllSessions",
            params={
                "jobIdFilter": job_id,
                "orderColumn": "CreationTime",
                "orderAsc": False,
                "limit": 5,
            },
        ),
    )
    return render_partial(
        request,
        "partials/job_panel.html",
        conn=conn,
        job=job,
        sessions=(sessions or {}).get("data", []),
        problem=problem,
    )


def _job_finish_hook(conn: Connection, job_id: str) -> Callable[[SessionSnapshot], Awaitable[None]]:
    async def refresh(_: SessionSnapshot) -> None:
        # One honest API call so the row shows the server's view of the job afterwards.
        states = await conn.client.request("GetAllJobsStates", params={"idFilter": job_id})
        if states["data"]:
            conn.job_cache[job_id] = states["data"][0]

    return refresh


async def _start_session(
    request: Request, conn: Connection, job_id: str, operation_id: str, verb: str
) -> Response:
    body = {"performActiveFull": False} if operation_id == "StartJob" else None
    session = await conn.client.request(operation_id, path_params={"id": job_id}, json=body)
    snapshot = conn.tracker.track(session, on_finish=_job_finish_hook(conn, job_id))
    conn.dock_session = snapshot.id
    job = conn.job_cache.get(job_id, {"id": job_id, "name": session.get("name", "")})
    return render_partial(
        request,
        "partials/track.html",
        conn=conn,
        snap=snapshot,
        focus=True,
        oob_job=job,
        oob_snapshot=snapshot,
        toast=toast_html(f"{verb} {job.get('name', 'the job')}. Watching the session."),
    )


@router.post("/ui/jobs/{job_id}/start", response_class=HTMLResponse)
async def start_job(request: Request, conn: Conn, job_id: str) -> Response:
    return await _start_session(request, conn, job_id, "StartJob", "Started")


@router.post("/ui/jobs/{job_id}/retry", response_class=HTMLResponse)
async def retry_job(request: Request, conn: Conn, job_id: str) -> Response:
    return await _start_session(request, conn, job_id, "RetryJob", "Retrying")


@router.post("/ui/jobs/{job_id}/stop", response_class=HTMLResponse)
async def stop_job(request: Request, conn: Conn, job_id: str) -> Response:
    session = await conn.client.request("StopJob", path_params={"id": job_id})
    snapshot = conn.tracker.track(session, on_finish=_job_finish_hook(conn, job_id))
    job = conn.job_cache.get(job_id, {"id": job_id, "name": session.get("name", "")})
    response = render_partial(
        request,
        "partials/job_row.html",
        conn=conn,
        job=job,
        snapshot=snapshot,
        stopping=True,
        toast=toast_html(f"Stopping {job.get('name', 'the job')}…"),
    )
    return response


# ---------------------------------------------------------------- sessions


@router.get("/ui/sessions/{session_id}/track", response_class=HTMLResponse)
async def session_track(request: Request, conn: Conn, session_id: str) -> Response:
    snapshot = conn.tracker.latest(session_id)
    if snapshot is None:
        session = await conn.client.request("GetSession", path_params={"id": session_id})
        snapshot = conn.tracker.track(session)
    return render_partial(request, "partials/track.html", conn=conn, snap=snapshot, focus=False)


@router.post("/ui/sessions/{session_id}/retry-now")
async def session_retry_now(conn: Conn, session_id: str) -> Response:
    conn.tracker.retry_now(session_id)
    return Response(status_code=204)


async def _sessions_page(conn: Connection, page: int) -> dict[str, Any]:
    result = await conn.client.request(
        "GetAllSessions",
        params={
            "skip": (page - 1) * SESSIONS_PER_PAGE,
            "limit": SESSIONS_PER_PAGE,
            "orderColumn": "CreationTime",
            "orderAsc": False,
        },
    )
    total = result["pagination"]["total"]
    return {
        "sessions": result["data"],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / SESSIONS_PER_PAGE)),
        "first": (page - 1) * SESSIONS_PER_PAGE + 1 if total else 0,
        "last": min(page * SESSIONS_PER_PAGE, total),
    }


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, state: State, conn: Conn, page: int = 1) -> Response:
    data, problem = await attempt(conn, state, lambda: _sessions_page(conn, max(page, 1)))
    return render(
        request, "sessions.html", conn=conn, active="sessions", table=data, problem=problem
    )


@router.get("/ui/sessions/table", response_class=HTMLResponse)
async def sessions_table(request: Request, conn: Conn, page: int = 1) -> Response:
    data = await _sessions_page(conn, max(page, 1))
    return render_partial(request, "partials/sessions_table.html", conn=conn, table=data)


@router.get("/ui/sessions/{session_id}/logs", response_class=HTMLResponse)
async def session_logs(request: Request, conn: Conn, session_id: str) -> Response:
    logs = await conn.client.request("GetSessionLogs", path_params={"id": session_id})
    return render_partial(
        request,
        "partials/session_logs.html",
        conn=conn,
        records=logs.get("records", []),
        session_id=session_id,
    )


# ---------------------------------------------------------------- repositories


async def _repositories(conn: Connection, sort: str) -> dict[str, Any]:
    column, ascending, _ = REPO_SORTS.get(sort, REPO_SORTS["name"])
    result = await conn.client.request(
        "GetAllRepositoriesStates", params={"orderColumn": column, "orderAsc": ascending}
    )
    return {"repositories": result["data"], "sort": sort if sort in REPO_SORTS else "name"}


@router.get("/repositories", response_class=HTMLResponse)
async def repositories_page(
    request: Request, state: State, conn: Conn, sort: str = "name"
) -> Response:
    data, problem = await attempt(conn, state, lambda: _repositories(conn, sort))
    return render(
        request,
        "repositories.html",
        conn=conn,
        active="repositories",
        data=data,
        problem=problem,
        sorts=REPO_SORTS,
    )


@router.get("/ui/repositories/cards", response_class=HTMLResponse)
async def repository_cards(request: Request, conn: Conn, sort: str = "name") -> Response:
    data = await _repositories(conn, sort)
    return render_partial(request, "partials/repo_cards.html", conn=conn, data=data)


# ---------------------------------------------------------------- incident response


async def _events(conn: Connection) -> list[dict[str, Any]]:
    result = await conn.client.request(
        "ViewSuspiciousActivityEvents",
        params={"orderColumn": "DetectionTimeUtc", "orderAsc": False, "limit": 10},
    )
    return list(result["data"])


def _incident_context(conn: Connection, state: AppState) -> dict[str, Any]:
    incident = conn.incident
    return {
        "incident": incident,
        "quick": conn.tracker.latest(incident.quick_session) if incident.quick_session else None,
        "scan": conn.tracker.latest(incident.scan_session) if incident.scan_session else None,
    }


@router.get("/incident", response_class=HTMLResponse)
async def incident_page(request: Request, state: State, conn: Conn) -> Response:
    events, problem = await attempt(conn, state, lambda: _events(conn))
    return render(
        request,
        "incident.html",
        conn=conn,
        active="incident",
        events=events,
        problem=problem,
        **_incident_context(conn, state),
    )


def _stepper(request: Request, conn: Connection, state: AppState, **extra: Any) -> Response:
    return render_partial(
        request, "partials/stepper.html", conn=conn, **_incident_context(conn, state), **extra
    )


@router.post("/ui/incident/event", response_class=HTMLResponse)
async def incident_select_event(
    request: Request, state: State, conn: Conn, event_id: Annotated[str, Form()]
) -> Response:
    events = await _events(conn)
    event = next((e for e in events if e["id"] == event_id), None)
    if event is None:
        return _stepper(
            request,
            conn,
            state,
            toast=toast_html("That object no longer exists. The list has been refreshed."),
        )
    conn.incident = type(conn.incident)(event=event)
    backup_object_id = (event.get("machine") or {}).get("backupObjectId")
    if backup_object_id:
        conn.incident.backup_object = await conn.client.request(
            "GetBackupObject", path_params={"id": backup_object_id}
        )
    return _stepper(request, conn, state)


def _incident_hook(conn: Connection, which: str) -> Callable[[SessionSnapshot], Awaitable[None]]:
    async def record(snapshot: SessionSnapshot) -> None:
        if which == "quick" and conn.incident.quick_session == snapshot.id:
            conn.incident.quick_result = snapshot.result
        elif which == "scan" and conn.incident.scan_session == snapshot.id:
            conn.incident.scan_result = snapshot.result

    return record


async def _find_agent_session(conn: Connection, job_id: str) -> dict[str, Any]:
    """Agent Quick Backup answers with a job id only; its session is the newest for that job."""
    result = await conn.client.request(
        "GetAllSessions",
        params={
            "jobIdFilter": job_id,
            "orderColumn": "CreationTime",
            "orderAsc": False,
            "limit": 1,
        },
    )
    if not result["data"]:
        raise VbrError("The quick backup started, but its session isn't listed yet.")
    return dict(result["data"][0])


@router.post("/ui/incident/quick-backup", response_class=HTMLResponse)
async def incident_quick_backup(request: Request, state: State, conn: Conn) -> Response:
    incident = conn.incident
    if incident.step != 2 or incident.backup_object is None:
        return _stepper(request, conn, state)
    try:
        operation_id, body = quick_backup_request(incident.backup_object)
    except UnsupportedMachine as exc:
        incident.error = str(exc)
        return _stepper(request, conn, state)
    session = await conn.client.request(operation_id, json=body)
    if "id" not in session:
        session = await _find_agent_session(conn, session["jobId"])
    incident.quick_session, incident.quick_result, incident.error = session["id"], None, None
    conn.tracker.track(session, on_finish=_incident_hook(conn, "quick"))
    return _stepper(request, conn, state)


@router.post("/ui/incident/scan", response_class=HTMLResponse)
async def incident_scan(request: Request, state: State, conn: Conn) -> Response:
    incident = conn.incident
    if incident.step != 3 or incident.backup_object is None or incident.event is None:
        return _stepper(request, conn, state)
    body = backup_scan_request(
        incident.backup_object["backupId"], incident.event["machine"]["backupObjectId"]
    )
    session = await conn.client.request("StartMalwareBackupScan", json=body)
    incident.scan_session, incident.scan_result = session["id"], None
    conn.tracker.track(session, on_finish=_incident_hook(conn, "scan"))
    return _stepper(request, conn, state)


@router.post("/ui/incident/reset", response_class=HTMLResponse)
async def incident_reset(request: Request, state: State, conn: Conn) -> Response:
    conn.incident = type(conn.incident)()
    return hx_redirect(request, "/incident")


@router.get("/ui/incident/stepper", response_class=HTMLResponse)
async def incident_stepper(request: Request, state: State, conn: Conn) -> Response:
    return _stepper(request, conn, state)


# ---------------------------------------------------------------- settings


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, state: State, conn: Conn) -> Response:
    return render(
        request,
        "settings.html",
        conn=conn,
        active="settings",
        scenarios=[state.mock.scenarios[key] for key in SCENARIO_KEYS],
        current_scenario=state.mock.scenario.key,
        poll_choices=POLL_CHOICES,
        poll_seconds=int(state.poll_seconds),
    )


@router.post("/ui/settings/scenario", response_class=HTMLResponse)
async def set_scenario(
    request: Request, state: State, conn: Conn, scenario: Annotated[str, Form()]
) -> Response:
    if scenario in SCENARIO_KEYS:
        state.mock.set_scenario(scenario)
    title = state.mock.scenario.title
    chip = f'<span id="scenario-chip" hx-swap-oob="innerHTML">Scenario: {escape(title)}</span>'
    # str(): `Markup + str` would escape the chip markup.
    return Response(str(toast_html(f"Mock scenario: {title}.")) + chip, media_type="text/html")


@router.post("/ui/settings/poll", response_class=HTMLResponse)
async def set_poll(
    request: Request, state: State, conn: Conn, seconds: Annotated[int, Form()]
) -> Response:
    if seconds in POLL_CHOICES:
        state.set_poll_seconds(seconds)
    return Response(
        toast_html(f"Polling every {int(state.poll_seconds)} s."), media_type="text/html"
    )


@router.post("/ui/settings/seed-event", response_class=HTMLResponse)
async def seed_event(request: Request, state: State, conn: Conn) -> Response:
    """Lab-only: create a malware event for the demo machine (PLAN §7.5.5)."""
    objects = await conn.client.request(
        "GetAllBackupObjects", params={"nameFilter": "FS-02", "limit": 1}
    )
    machine: dict[str, Any] = {"fqdn": "FS-02"}
    if objects.get("data"):
        machine["backupObjectId"] = objects["data"][0]["id"]
    detected = (datetime.now(UTC) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await conn.client.request(
        "CreateSuspiciousActivityEvent",
        json={
            "detectionTimeUtc": detected,
            "machine": machine,
            "details": "Suspicious file activity on FS-02 (seeded by VBR Pulse for a demo).",
            "engine": "VBR Pulse lab seed",
            "severity": "Suspicious",
        },
    )
    return Response(toast_html("Seeded a malware event for FS-02."), media_type="text/html")


# ---------------------------------------------------------------- inspector + token


@router.get("/ui/inspector/{event_id}", response_class=HTMLResponse)
async def inspector_detail(request: Request, state: State, conn: Conn, event_id: str) -> Response:
    event = next((e for e in state.bus.recent() if e.id == event_id), None)
    if event is None:
        return HTMLResponse(
            '<div class="insp-body"><p class="muted">That call has left the '
            "inspector's buffer.</p></div>"
        )
    return render_partial(request, "partials/inspector_detail.html", conn=conn, e=event)


@router.get("/ui/inspector/{event_id}/full", response_class=HTMLResponse)
async def inspector_full(request: Request, state: State, conn: Conn, event_id: str) -> Response:
    event = next((e for e in state.bus.recent() if e.id == event_id), None)
    return render_partial(request, "partials/inspector_full.html", conn=conn, event=event)


@router.post("/ui/inspector/clear", response_class=HTMLResponse)
async def inspector_clear(request: Request, state: State, conn: Conn) -> Response:
    state.bus.clear()
    return HTMLResponse("")


@router.get("/ui/token", response_class=HTMLResponse)
async def token_ring(request: Request, conn: Conn) -> Response:
    # The ring asks for this when the refresh point passes, so an idle UI stays signed in.
    await conn.client.refresh_if_due()
    return render_partial(request, "partials/token_ring.html", conn=conn)


__all__ = ["NotSignedIn", "router"]

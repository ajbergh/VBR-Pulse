"""Server-Sent Events: the live inspector and live session tracks (PLAN §4.1, §7.5.3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from markupsafe import escape

from pulse.inspector.bus import InspectorEvent
from pulse.vbr.sessions import SessionSnapshot
from pulse.web import views
from pulse.web.render import (
    connection,
    pulse_state,
    render_string,
    token_context,
)
from pulse.web.state import AppState, Connection

router = APIRouter()
KEEPALIVE_SECONDS = 15.0
_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(event: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in html.splitlines() or [""])
    return f"event: {event}\n{data}\n\n"


def inspector_payload(event: InspectorEvent, state: AppState) -> str:
    row = views.row_for(event, state.bus.recent())
    html = render_string("partials/inspector_row.html", row=row)
    if event.group:
        # Grouped polling rows move to the top: delete the old one, prepend the new one.
        html = f'<div id="{row.dom_id}" hx-swap-oob="delete"></div>' + html
    return html


def track_payload(snapshot: SessionSnapshot, conn: Connection, state: AppState) -> str:
    html = render_string("partials/track_inner.html", snap=snapshot, conn=conn)
    job = conn.job_cache.get(snapshot.job_id)
    if job is not None and snapshot.session_type != "MalwareDetection":
        html += render_string(
            "partials/job_row.html", job=job, snapshot=snapshot, oob=True, conn=conn
        )
    if snapshot.ended and snapshot.id in (conn.incident.quick_session, conn.incident.scan_session):
        html += render_string(
            "partials/stepper.html",
            conn=conn,
            incident=conn.incident,
            oob=True,
            quick=conn.tracker.latest(conn.incident.quick_session or ""),
            scan=conn.tracker.latest(conn.incident.scan_session or ""),
        )
    return html


@router.get("/events/inspector")
async def inspector_events(
    request: Request, conn: Annotated[Connection, Depends(connection)]
) -> StreamingResponse:
    state = pulse_state(request)

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        async with state.bus.subscribe() as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield sse("row", inspector_payload(event, state))
                if event.operation_id in ("CreateToken", "Logout"):
                    ring = render_string("partials/token_ring.html", token=token_context(conn))
                    yield sse("token", ring)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_HEADERS)


@router.get("/events/session/{session_id}")
async def session_events(
    request: Request, session_id: str, conn: Annotated[Connection, Depends(connection)]
) -> StreamingResponse:
    state = pulse_state(request)
    if conn.tracker.latest(session_id) is None:
        conn.tracker.track(await conn.client.request("GetSession", path_params={"id": session_id}))

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        announced: tuple[str, str] | None = None
        async with conn.tracker.subscribe(session_id) as queue:
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                html = track_payload(snapshot, conn, state)
                if (snapshot.state, snapshot.result) != announced:
                    # One persistent live region, updated only when the state changes.
                    announced = (snapshot.state, snapshot.result)
                    words = f"{snapshot.name}: {snapshot.state}"
                    if snapshot.ended:
                        words += f", result {snapshot.result}"
                    html += f'<div id="announcer" hx-swap-oob="innerHTML">{escape(words)}</div>'
                yield sse("update", html)
                if snapshot.ended:
                    yield sse("done", "")
                    return

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_HEADERS)

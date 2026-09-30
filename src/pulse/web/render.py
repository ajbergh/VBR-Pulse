"""Template rendering, htmx response helpers and request dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from pulse.vbr.errors import (
    VbrError,
    VbrForbidden,
    VbrNotFound,
    VbrRequestInvalid,
    VbrServerError,
)
from pulse.vbr.operations import SPEC_VERSION
from pulse.web import views
from pulse.web.state import COOKIE, AppState, Connection

WEB = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=WEB / "templates")
env = templates.env
env.globals.update(
    api_version=SPEC_VERSION,
    JOB_STATUS=views.JOB_STATUS,
    RESULT=views.RESULT,
    JOB_TYPES=views.JOB_TYPES,
    SESSION_TYPES=views.SESSION_TYPES,
    REPOSITORY_TYPES=views.REPOSITORY_TYPES,
    json_html=views.json_html,
    curl=views.curl,
    track_states=views.track_states,
)
env.filters.update(
    clock=views.clock_time,
    clock_seconds=views.clock_seconds,
    duration=views.duration,
    size_gb=views.size_gb,
    short_id=views.short_id,
    display_path=views.display_path,
    display_url=views.display_url,
)

# Which profile to suggest after a 403, by the kind of operation that was refused.
_SUGGEST = {
    "StartJob": "ops",
    "StopJob": "ops",
    "RetryJob": "ops",
    "GetAllJobsStates": "ops",
    "GetAllRepositoriesStates": "ops",
    "StartMalwareBackupScan": "ir",
    "CreateSuspiciousActivityEvent": "ir",
    "StartHyperVQuickBackupJob": "ir",
    "StartVSphereQuickBackupJob": "ir",
    "StartAgentQuickBackupJob": "ir",
}
_PROFILE_WORDS = {"ops": "operator", "ir": "incident", "view": "viewer"}


class NotSignedIn(Exception):
    pass


@dataclass(frozen=True)
class Callout:
    kind: str  # "forbidden" | "error" | "info"
    title: str
    message: str
    code: str | None = None
    suggest_profile: str | None = None


def pulse_state(request: Request) -> AppState:
    state: AppState = request.app.state.pulse
    return state


def connection(request: Request) -> Connection:
    conn = pulse_state(request).get(request.cookies.get(COOKIE))
    if conn is None:
        raise NotSignedIn
    return conn


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def hx_redirect(request: Request, url: str) -> Response:
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def set_session_cookie(response: Response, conn_id: str) -> None:
    # HttpOnly + SameSite=Strict (PLAN §9). No Secure flag: Pulse serves http on loopback.
    response.set_cookie(COOKIE, conn_id, httponly=True, samesite="strict", path="/")


def token_context(conn: Connection | None) -> dict[str, Any] | None:
    info = conn.client.token_info() if conn else None
    if info is None:
        return None
    now_ms = time.time() * 1000
    return {
        "lifetime": info.lifetime_seconds,
        "expires_at_ms": int(now_ms + info.seconds_left * 1000),
        "refresh_at_ms": int(now_ms + info.refresh_in_seconds * 1000),
        "seconds_left": int(info.seconds_left),
        "refresh_in": int(info.refresh_in_seconds),
    }


def _base_context(request: Request, conn: Connection | None) -> dict[str, Any]:
    state = pulse_state(request)
    return {
        "app": state,
        "conn": conn,
        "role": state.role_of(conn) if conn else None,
        "token": token_context(conn),
        "insecure_tls": state.settings.lab_insecure_tls and (conn is None or conn.mode == "live"),
        "scenario": state.mock.scenario if conn and conn.mode == "mock" else None,
    }


def render(request: Request, name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    conn = context.pop("conn", None)
    state = pulse_state(request)
    ctx = _base_context(request, conn)
    ctx["rows"] = views.inspector_rows(state.bus.recent()) if conn else []
    ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def render_partial(
    request: Request, name: str, *, toast: Markup | None = None, **context: Any
) -> HTMLResponse:
    conn = context.pop("conn", None)
    ctx = _base_context(request, conn)
    ctx.update(context)
    html = env.get_template(name).render(request=request, **ctx)
    # str() on both sides: `str + Markup` would escape the rendered template.
    return HTMLResponse(str(html) + str(toast or ""))


def render_string(name: str, **context: Any) -> str:
    return env.get_template(name).render(**context)


def toast_html(
    message: str, kind: str = "info", *, retry: tuple[str, str, str | None] | None = None
) -> Markup:
    """An out-of-band toast; `retry` is (method, url, target) for a "Try again" button."""
    button = ""
    if retry is not None:
        method, url, target = retry
        hx_target = f' hx-target="#{escape(target)}"' if target else ' hx-swap="none"'
        button = (
            f'<button class="btn-text" hx-{method.lower()}="{escape(url)}"{hx_target}>'
            "Try again</button>"
        )
    icon = {"error": "failed", "forbidden": "lock"}.get(kind, "info")
    return Markup(
        '<div id="toasts" hx-swap-oob="beforeend">'
        f'<div class="toast toast-{escape(kind)}" role="status">'
        f'<svg class="icon" aria-hidden="true"><use href="#i-{icon}"></use></svg>'
        f"<span>{escape(message)}</span>{button}"
        '<button class="toast-close" aria-label="Dismiss">×</button></div></div>'
    )


def callout_for(exc: VbrError, conn: Connection, state: AppState) -> Callout:
    if isinstance(exc, VbrForbidden):
        suggest = _SUGGEST.get(exc.operation_id or "")
        if suggest == conn.profile.name:
            suggest = None
        return Callout(
            "forbidden",
            "This account's role can't do that.",
            views.forbidden_message(conn.username, state.role_of(conn), exc.operation_id),
            exc.error_code,
            suggest
            if suggest and any(p.name == suggest for p in state.profiles(conn.mode))
            else None,
        )
    if isinstance(exc, VbrRequestInvalid):
        return Callout("error", "Pulse didn't send the request", exc.message, exc.error_code)
    if isinstance(exc, VbrNotFound):
        return Callout("error", "Not found", "That object no longer exists.", exc.error_code)
    if isinstance(exc, VbrServerError):
        return Callout("error", f"Can't reach {conn.host}", exc.message, exc.error_code)
    return Callout("error", "The server refused the request", exc.message, exc.error_code)


def callout_html(callout: Callout) -> str:
    return render_string("partials/callout.html", callout=callout)


env.globals["profile_word"] = _PROFILE_WORDS

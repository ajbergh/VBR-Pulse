"""FastAPI app factory (PLAN §4.2, Phase 3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup

from pulse.config import Settings, load_settings
from pulse.mock.state import MockVbr
from pulse.vbr.errors import VbrError, VbrNotFound, VbrServerError
from pulse.web import events, routes
from pulse.web.render import (
    WEB,
    NotSignedIn,
    callout_for,
    callout_html,
    connection,
    hx_redirect,
    is_htmx,
    pulse_state,
    render,
    toast_html,
)
from pulse.web.state import COOKIE, AppState

# Only same-origin scripts; htmx eval is disabled via its config meta tag.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


def create_app(settings: Settings | None = None, *, mock: MockVbr | None = None) -> FastAPI:
    settings = settings or load_settings()
    state = AppState(settings, mock=mock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await state.aclose()  # logs out every connection (PLAN §9)

    app = FastAPI(
        title="VBR Pulse", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.pulse = state
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    app.include_router(routes.router)
    app.include_router(events.router)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reject cross-site form posts even if a browser ignores SameSite (PLAN §9).
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if request.method == "POST" and origin and origin.split("://", 1)[-1] != host:
            return Response("Cross-origin request refused.", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(request: Request, exc: NotSignedIn) -> Response:
        if request.url.path.startswith("/events/"):
            return Response(status_code=204)  # tells EventSource to stop reconnecting
        return hx_redirect(request, "/signin")

    @app.exception_handler(VbrError)
    async def vbr_error(request: Request, exc: VbrError) -> Response:
        state = pulse_state(request)
        if exc.status == 401:
            # The client already refreshed once; the session is gone (PLAN §5.3).
            await state.disconnect(request.cookies.get(COOKIE))
            response = hx_redirect(request, "/signin?expired=true")
            response.delete_cookie(COOKIE)
            return response
        try:
            conn = connection(request)
        except NotSignedIn:
            return hx_redirect(request, "/signin")
        callout = callout_for(exc, conn, state)
        if not is_htmx(request):
            return render(request, "error.html", conn=conn, problem=callout, status_code=502)

        headers = {"HX-Reswap": "none"}
        if isinstance(exc, VbrNotFound):
            body = toast_html("That object no longer exists. The list has been refreshed.")
            headers["HX-Trigger"] = "pulse:refresh"
        elif isinstance(exc, VbrServerError):
            retry = (
                request.method,
                str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
                request.headers.get("HX-Target"),
            )
            body = toast_html(exc.message, "error", retry=retry)
        elif callout.kind == "forbidden":
            body = Markup(
                '<div id="callouts" hx-swap-oob="innerHTML">' + callout_html(callout) + "</div>"
            )
        else:
            body = toast_html(f"{callout.message}", "error")
        return HTMLResponse(body, headers=headers)

    return app

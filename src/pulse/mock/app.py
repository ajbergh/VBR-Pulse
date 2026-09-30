"""The mock VBR server as an ASGI app (PLAN §6).

Routes are resolved by operationId from the same generated table the client uses, so
the mock can't drift from the spec's paths. Mounted in-process with
httpx.ASGITransport: `VbrClient(MOCK_BASE_URL, ..., transport=mock_transport(mock))`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI
from starlette.requests import Request as HttpRequest
from starlette.responses import JSONResponse, Response

from pulse.mock.state import MockError, MockVbr, Request
from pulse.vbr.errors import VbrRequestInvalid
from pulse.vbr.operations import OPERATIONS, SPEC_VERSION
from pulse.vbr.spec import validate_request_body

# Same host name as the fixture server, so inspector URLs look identical to live mode.
MOCK_BASE_URL = "https://vbr01.lab.local"


class MockConnectionDropped(httpx.ConnectError):
    """Raised inside the app for `inject_fault(..., status=0)`; surfaces as a network error."""


def _error(exc: MockError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status)


async def _parse_body(request: HttpRequest, operation_id: str) -> Any:
    raw = await request.body()
    if operation_id == "CreateToken":
        return dict(parse_qsl(raw.decode("utf-8")))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise MockError(400, "UnexpectedContent", "The request body isn't valid JSON.") from exc


def _endpoint(mock: MockVbr, operation_id: str) -> Any:
    op = OPERATIONS[operation_id]
    status, handler = mock.handlers().get(operation_id, (200, None))

    async def endpoint(request: HttpRequest) -> Response:
        mock.calls[operation_id] += 1
        try:
            if request.headers.get("x-api-version") != SPEC_VERSION:
                raise MockError(
                    400,
                    "UnexpectedContent",
                    "The x-api-version header is missing or unsupported "
                    f"(expected {SPEC_VERSION}).",
                )
            fault = mock.take_fault(operation_id)
            if fault == 0:
                raise MockConnectionDropped("Connection reset by the mock (injected fault)")
            if fault is not None:
                code = "ServiceUnavailable" if fault == 503 else "UnknownError"
                raise MockError(fault, code, "Injected fault.")

            body = await _parse_body(request, operation_id)
            if operation_id == "CreateToken":
                return JSONResponse(mock.create_token(body or {}), status_code=200)

            username, token = mock.authenticate(request.headers.get("authorization"))
            mock.authorize(username, operation_id)
            if handler is None:
                raise MockError(501, "NotImplemented", f"{operation_id} isn't mocked.")
            if body is not None:
                try:
                    validate_request_body(operation_id, op.method, op.path, body)
                except VbrRequestInvalid as exc:
                    raise MockError(400, "UnexpectedContent", exc.message) from exc
            result = handler(
                Request(
                    username=username,
                    token=token,
                    path=dict(request.path_params),
                    query=dict(request.query_params),
                    body=body,
                )
            )
        except MockError as exc:
            return _error(exc)
        return JSONResponse(result, status_code=status)

    return endpoint


def create_mock_app(mock: MockVbr) -> FastAPI:
    app = FastAPI(title="VBR Pulse mock", openapi_url=None, docs_url=None, redoc_url=None)
    app.state.mock = mock
    wanted = {"CreateToken", "GetAllJobs", *mock.handlers()}
    # Register fixed paths before templated ones so /jobs/states beats /jobs/{id}.
    for operation_id in sorted(wanted, key=lambda o: OPERATIONS[o].path.count("{")):
        op = OPERATIONS[operation_id]
        app.router.add_route(
            op.path, _endpoint(mock, operation_id), methods=[op.method], name=operation_id
        )

    async def not_found(request: HttpRequest) -> Response:
        return JSONResponse(
            {"errorCode": "NotFound", "message": f"{request.url.path} isn't mocked."},
            status_code=404,
        )

    app.router.add_route("/{path:path}", not_found, methods=["GET", "POST", "PUT", "DELETE"])
    return app


def mock_transport(mock: MockVbr) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=create_mock_app(mock))

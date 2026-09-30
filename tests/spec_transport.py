"""An httpx transport wrapper that checks every response against the 1.3-rev2 spec."""

from __future__ import annotations

import json
import re
from collections import Counter

import httpx

from pulse.vbr.operations import OPERATIONS, Operation
from pulse.vbr.spec import validate_response_body

_ROUTES: list[tuple[str, re.Pattern[str], Operation]] = sorted(
    (
        (op.method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", op.path) + "$"), op)
        for op in OPERATIONS.values()
    ),
    key=lambda row: row[2].path.count("{"),
)


def operation_for(method: str, path: str) -> Operation:
    for route_method, pattern, op in _ROUTES:
        if route_method == method and pattern.match(path):
            return op
    raise LookupError(f"No operation for {method} {path}")


class SpecCheckingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.checked: Counter[str] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        op = operation_for(request.method, request.url.path)
        body = json.loads(content) if content else None
        validate_response_body(op.method, op.path, response.status_code, body)
        self.checked[f"{op.operation_id} {response.status_code}"] += 1
        return httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()

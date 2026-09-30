"""VbrClient: the only way VBR Pulse talks to Veeam Backup & Replication (PLAN §4.4).

- OAuth 2.0 password grant, proactive refresh at 80 % of `expires_in`, one reactive
  refresh on 401.
- Every call resolved by operationId, sent with `x-api-version`, and published to
  the Inspector bus after redaction.
- Idempotent GETs retried on 5xx / network errors; POSTs are never retried
  (the one exception is replaying a request the server rejected with 401, which
  it therefore never executed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from pulse.inspector.bus import InspectorBus, InspectorEvent, Mode
from pulse.inspector.redact import redact_body, redact_headers, redact_url
from pulse.vbr.errors import (
    VbrBadRequest,
    VbrError,
    VbrNetworkError,
    VbrTlsError,
    VbrUnauthorized,
    error_from_response,
)
from pulse.vbr.operations import OPERATIONS, SPEC_VERSION, Operation
from pulse.vbr.spec import validate_request_body

API_VERSION = SPEC_VERSION
REFRESH_RATIO = 0.8
DEFAULT_PAGE_LIMIT = 200
MAX_INSPECTOR_BODY_BYTES = 256 * 1024

_INSPECTOR_HEADERS = ("x-api-version", "authorization", "content-type", "accept")


@dataclass(frozen=True)
class Credentials:
    username: str
    password: SecretStr


CredentialSupplier = Callable[[], Credentials]


@dataclass
class _Tokens:
    access: SecretStr
    refresh: SecretStr
    expires_in: int
    obtained_at: float  # client clock (monotonic)


@dataclass(frozen=True)
class TokenInfo:
    """Token lifetime details that are safe to show in the UI (no token material)."""

    lifetime_seconds: int
    seconds_left: float
    refresh_in_seconds: float
    expires_at: datetime
    refreshes_at: datetime


def _tls_verify(verify: bool | str) -> ssl.SSLContext | bool:
    if verify is False:
        return False
    if isinstance(verify, str) and verify:
        return ssl.create_default_context(cafile=verify)
    # System trust store, so lab CAs deployed to the laptop (e.g. via GPO) just work.
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _normalise_base_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url.removesuffix("/api")


def _render_path(op: Operation, path_params: Mapping[str, Any] | None) -> str:
    path = op.path
    for name, value in (path_params or {}).items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    if "{" in path:
        raise ValueError(f"{op.operation_id}: missing path parameters for {op.path}")
    return path


def _query(params: Mapping[str, Any] | None) -> dict[str, str | int | float]:
    out: dict[str, str | int | float] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, int | float | str):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _is_tls_failure(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


class VbrClient:
    def __init__(
        self,
        base_url: str,
        credentials: CredentialSupplier,
        *,
        api_version: str = API_VERSION,
        verify: bool | str = True,
        inspector: InspectorBus | None = None,
        mode: Mode = "live",
        transport: httpx.AsyncBaseTransport | None = None,
        validate_bodies: bool = True,
        max_get_attempts: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = _normalise_base_url(base_url)
        self.api_version = api_version
        self.mode: Mode = mode
        self.inspector = inspector
        self._credentials = credentials
        self._validate_bodies = validate_bodies
        self._max_get_attempts = max(1, max_get_attempts)
        self._clock = clock
        self._sleep = sleep
        self._tokens: _Tokens | None = None
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            verify=_tls_verify(verify) if transport is None else True,
            timeout=httpx.Timeout(30.0, connect=5.0),
            transport=transport,
            headers={"x-api-version": api_version, "Accept": "application/json"},
        )

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.logout()
        await self._http.aclose()

    @property
    def is_authenticated(self) -> bool:
        return self._tokens is not None

    def token_info(self) -> TokenInfo | None:
        tokens = self._tokens
        if tokens is None:
            return None
        elapsed = self._clock() - tokens.obtained_at
        left = max(0.0, tokens.expires_in - elapsed)
        refresh_in = max(0.0, tokens.expires_in * REFRESH_RATIO - elapsed)
        now = datetime.now(UTC)
        return TokenInfo(
            lifetime_seconds=tokens.expires_in,
            seconds_left=left,
            refresh_in_seconds=refresh_in,
            expires_at=now + timedelta(seconds=left),
            refreshes_at=now + timedelta(seconds=refresh_in),
        )

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """`CreateToken` with the password grant."""
        async with self._token_lock:
            await self._login_locked()

    async def refresh(self) -> None:
        """`CreateToken` with the refresh_token grant (falls back to a fresh login)."""
        async with self._token_lock:
            await self._refresh_locked()

    async def logout(self) -> None:
        """`Logout`, then forget the tokens. Never raises: used on shutdown."""
        tokens, self._tokens = self._tokens, None
        if tokens is None:
            return
        op = OPERATIONS["Logout"]
        # The Logout row still shows in the inspector; a failure here isn't actionable.
        with contextlib.suppress(VbrError):
            await self._send(op, op.path, access_token=tokens.access)

    async def _login_locked(self) -> None:
        creds = self._credentials()
        await self._token_call(
            {
                "grant_type": "password",
                "username": creds.username,
                "password": creds.password.get_secret_value(),
            }
        )

    async def _refresh_locked(self) -> None:
        if self._tokens is None:
            await self._login_locked()
            return
        try:
            await self._token_call(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.refresh.get_secret_value(),
                }
            )
        except (VbrUnauthorized, VbrBadRequest):
            # Refresh tokens are single-use and expire after 24 h; start over.
            self._tokens = None
            await self._login_locked()

    async def _token_call(self, form: dict[str, str]) -> None:
        op = OPERATIONS["CreateToken"]
        response = await self._send(op, op.path, form=form)
        body = _parse_body(response)
        if response.is_error:
            raise error_from_response(response.status_code, body, op.operation_id)
        if not isinstance(body, dict) or "access_token" not in body:
            raise VbrError("The token response had no access_token.", operation_id=op.operation_id)
        self._tokens = _Tokens(
            access=SecretStr(body["access_token"]),
            refresh=SecretStr(body.get("refresh_token", "")),
            expires_in=int(body.get("expires_in", 900)),
            obtained_at=self._clock(),
        )

    async def _ensure_token(self) -> SecretStr:
        async with self._token_lock:
            if self._tokens is None:
                await self._login_locked()
            else:
                elapsed = self._clock() - self._tokens.obtained_at
                if elapsed >= self._tokens.expires_in * REFRESH_RATIO:
                    await self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access

    async def _refresh_after_401(self, rejected: SecretStr) -> SecretStr:
        async with self._token_lock:
            # Another request may already have refreshed while we waited for the lock.
            if self._tokens is None or self._tokens.access is rejected:
                await self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access

    # ------------------------------------------------------------------ requests

    async def request(
        self,
        operation_id: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        group: str | None = None,
    ) -> Any:
        """Call an operation by operationId and return the parsed JSON body (or None)."""
        op = OPERATIONS.get(operation_id)
        if op is None:
            raise KeyError(f"Unknown operationId {operation_id!r} (not in the {SPEC_VERSION} spec)")
        if json is not None and self._validate_bodies:
            validate_request_body(op.operation_id, op.method, op.path, json)

        path = _render_path(op, path_params)
        query = _query(params)
        attempts = self._max_get_attempts if op.method == "GET" else 1
        token = await self._ensure_token()
        replayed_401 = False
        attempt = 1

        while True:
            try:
                response = await self._send(
                    op, path, params=query, json_body=json, access_token=token, group=group
                )
            except VbrNetworkError:
                if attempt >= attempts:
                    raise
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if response.status_code == 401 and not replayed_401:
                replayed_401 = True
                token = await self._refresh_after_401(token)
                continue
            if response.status_code >= 500 and attempt < attempts:
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            body = _parse_body(response)
            if response.is_error:
                raise error_from_response(response.status_code, body, op.operation_id)
            return body

    async def paginate(
        self,
        operation_id: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> AsyncIterator[Any]:
        """Yield every item of a paged collection, following `pagination.total`."""
        skip = 0
        while True:
            page = await self.request(
                operation_id,
                path_params=path_params,
                params={**(params or {}), "skip": skip, "limit": limit},
            )
            data = (page.get("data") or []) if isinstance(page, dict) else []
            for item in data:
                yield item
            total = int((page.get("pagination") or {}).get("total", 0)) if data else 0
            skip += len(data)
            if not data or skip >= total:
                return

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(8, 2 ** (attempt - 1)))

    # ------------------------------------------------------------------ wire

    async def _send(
        self,
        op: Operation,
        path: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        access_token: SecretStr | None = None,
        group: str | None = None,
    ) -> httpx.Response:
        headers = {}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token.get_secret_value()}"
        request = self._http.build_request(
            op.method,
            path,
            params=params or None,
            json=json_body,
            data=form,
            headers=headers,
        )
        started = time.perf_counter()
        try:
            response = await self._http.send(request)
        except httpx.HTTPError as exc:
            error = self._network_error(exc, op)
            self._publish(
                op, request, None, started, form=form, json_body=json_body, group=group, error=error
            )
            raise error from exc
        self._publish(op, request, response, started, form=form, json_body=json_body, group=group)
        return response

    def _network_error(self, exc: httpx.HTTPError, op: Operation) -> VbrNetworkError:
        url = urlsplit(self.base_url)
        host = url.hostname or self.base_url
        port = url.port or (443 if url.scheme == "https" else 80)
        if _is_tls_failure(exc):
            return VbrTlsError(
                "The server's certificate isn't trusted. Add its CA to PULSE_CA_BUNDLE.",
                operation_id=op.operation_id,
                error_code="TlsVerifyFailed",
            )
        if isinstance(exc, httpx.TimeoutException):
            return VbrNetworkError(
                f"{host} didn't answer in time.",
                operation_id=op.operation_id,
                error_code="Timeout",
            )
        return VbrNetworkError(
            f"Can't reach {host} on port {port}. Check the address or VPN.",
            operation_id=op.operation_id,
            error_code="NetworkError",
        )

    def _publish(
        self,
        op: Operation,
        request: httpx.Request,
        response: httpx.Response | None,
        started: float,
        *,
        form: dict[str, str] | None,
        json_body: Any,
        group: str | None,
        error: VbrError | None = None,
    ) -> None:
        if self.inspector is None:
            return
        duration_ms = round((time.perf_counter() - started) * 1000)
        headers = {
            name: value
            for name in _INSPECTOR_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        # Display the canonical header casing used in the docs.
        headers = {_display_header(k): v for k, v in headers.items()}

        response_body: Any = None
        truncated = False
        error_code = error.error_code if error else None
        if response is not None:
            parsed = _parse_body(response)
            if response.is_error and isinstance(parsed, dict):
                error_code = parsed.get("errorCode") or parsed.get("error")
            # Redact first, then truncate, so a cut-off body can never leak a secret.
            response_body = redact_body(parsed)
            if len(response.content) > MAX_INSPECTOR_BODY_BYTES:
                truncated = True
                text = json.dumps(response_body, ensure_ascii=False)
                response_body = text[:MAX_INSPECTOR_BODY_BYTES]

        self.inspector.publish(
            InspectorEvent(
                operation_id=op.operation_id,
                docs_url=op.docs_url,
                method=op.method,
                url=redact_url(str(request.url)),
                request_headers=redact_headers(headers),
                request_body=redact_body(form if form is not None else json_body),
                status=response.status_code if response is not None else None,
                duration_ms=duration_ms,
                response_body=response_body,
                response_truncated=truncated,
                error_code=error_code,
                error_message=error.message if error else None,
                group=group,
                mode=self.mode,
            )
        )


def _display_header(name: str) -> str:
    return {
        "authorization": "Authorization",
        "content-type": "Content-Type",
        "accept": "Accept",
    }.get(name, name)

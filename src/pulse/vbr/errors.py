"""VbrError hierarchy mapped from HTTP status codes (PLAN §5.3)."""

from __future__ import annotations

from typing import Any


class VbrError(Exception):
    """Base error for anything that goes wrong talking to VBR.

    `message` is safe to show on screen: it never contains credentials or tokens.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
        resource_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_code = error_code
        self.resource_id = resource_id
        self.operation_id = operation_id

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, error_code={self.error_code!r}, "
            f"operation_id={self.operation_id!r}, message={self.message!r})"
        )


class VbrBadRequest(VbrError):
    """400 — show `message` inline next to the form."""


class VbrUnauthorized(VbrError):
    """401 — the client already refreshed once; the caller should send the user to sign in."""


class VbrForbidden(VbrError):
    """403 — the signed-in account's role can't perform the operation."""


class VbrNotFound(VbrError):
    """404 — the object no longer exists."""


class VbrServerError(VbrError):
    """5xx or a network failure. GETs have already been retried per PLAN §4.4."""


class VbrNetworkError(VbrServerError):
    """The request never got an HTTP response (DNS, refused, timeout)."""


class VbrTlsError(VbrNetworkError):
    """The server's certificate failed verification."""


class VbrRequestInvalid(VbrError):
    """A request body failed validation against the OpenAPI spec before it was sent."""


_BY_STATUS: dict[int, type[VbrError]] = {
    400: VbrBadRequest,
    401: VbrUnauthorized,
    403: VbrForbidden,
    404: VbrNotFound,
}


def error_from_response(status: int, body: Any, operation_id: str | None) -> VbrError:
    """Build the right VbrError from an error body (`errorCode`, `message`, `resourceId`)."""
    error_code: str | None = None
    resource_id: str | None = None
    message = f"HTTP {status}"
    if isinstance(body, dict):
        error_code = body.get("errorCode") or None
        resource_id = body.get("resourceId") or None
        message = body.get("message") or message
        # The token endpoint answers with OAuth-style errors instead of the Error model.
        if "error" in body and not body.get("message"):
            error_code = error_code or str(body["error"])
            message = str(body.get("error_description") or body["error"])

    cls = _BY_STATUS.get(status) or (VbrServerError if status >= 500 else VbrError)
    return cls(
        message,
        status=status,
        error_code=error_code,
        resource_id=resource_id,
        operation_id=operation_id,
    )

"""Redaction for everything that enters the Inspector bus (PLAN §9).

Runs *before* an event is buffered, so nothing downstream ever sees a secret.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

SECRET_NAME = re.compile(r"secret|password|key|token", re.IGNORECASE)
TOKEN_NAME = re.compile(r"token", re.IGNORECASE)
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
MASK = "••••••"

# Short enough that showing the ends would reveal a meaningful share of the value.
_MIN_TOKEN_LEN_FOR_HINT = 24


def mask_token(value: str) -> str:
    """`eyJhbGciOi…Xk9Q` → `eyJhb…Xk9Q`: presenters can see a token changed without leaking it."""
    if len(value) < _MIN_TOKEN_LEN_FOR_HINT:
        return MASK
    return f"{value[:5]}…{value[-4:]}"


def _redact_value(name: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Mapping | list):
        # A secret-named container (e.g. `encryptionKey: {...}`) is hidden wholesale.
        return MASK
    if isinstance(value, str) and TOKEN_NAME.search(name):
        return mask_token(value)
    return MASK


def redact_body(body: Any) -> Any:
    """Return a deep copy of a JSON/form body with secret-named fields masked."""
    if isinstance(body, Mapping):
        return {
            key: _redact_value(str(key), value)
            if SECRET_NAME.search(str(key))
            else redact_body(value)
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [redact_body(item) for item in body]
    return body


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in ("authorization", "proxy-authorization"):
            scheme, _, credential = value.partition(" ")
            out[name] = f"{scheme} {mask_token(credential)}" if credential else MASK
        elif lower in SENSITIVE_HEADERS or SECRET_NAME.search(lower):
            out[name] = MASK
        else:
            out[name] = value
    return out


def redact_url(url: str) -> str:
    """Mask secret-named query parameters and any userinfo in a URL."""
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    pairs = []
    for pair in parts.query.split("&") if parts.query else []:
        name, sep, _ = pair.partition("=")
        pairs.append(f"{name}{sep}{MASK}" if SECRET_NAME.search(unquote(name)) else pair)
    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(pairs), parts.fragment))

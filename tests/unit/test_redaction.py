"""Redaction (PLAN §9): nothing secret ever reaches the inspector bus."""

from __future__ import annotations

import httpx
import respx

from pulse.inspector.bus import InspectorBus
from pulse.inspector.redact import (
    MASK,
    mask_token,
    redact_body,
    redact_headers,
    redact_url,
)
from pulse.vbr.client import VbrClient
from tests.conftest import PASSWORD, FakeClock, token_body


def test_mask_token_keeps_only_ends() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signatureXk9Q"
    assert mask_token(token) == "eyJhb…Xk9Q"


def test_short_tokens_fully_masked() -> None:
    assert mask_token("abc123") == MASK


def test_redact_body_all_secret_fields() -> None:
    body = {
        "grant_type": "password",
        "username": "svc-pulse-ops",
        "password": PASSWORD,
        "refresh_token": "r" * 40,
        "access_token": "a" * 40,
        "clientSecret": "s3cr3t",
        "apiKey": "k" * 10,
        "Password": "upper",
        "nested": {"secretValue": "x", "list": [{"passwordHint": "y", "name": "ok"}]},
        "encryptionKey": {"id": "k1"},
        "isKeyEnabled": True,
        "tokenCount": None,
    }

    red = redact_body(body)

    assert red["grant_type"] == "password"
    assert red["username"] == "svc-pulse-ops"
    assert red["password"] == MASK
    assert red["Password"] == MASK
    assert red["refresh_token"] == "rrrrr…rrrr"
    assert red["access_token"] == "aaaaa…aaaa"
    assert red["clientSecret"] == MASK
    assert red["apiKey"] == MASK
    assert red["nested"]["secretValue"] == MASK
    assert red["nested"]["list"][0] == {"passwordHint": MASK, "name": "ok"}
    assert red["encryptionKey"] == MASK
    assert red["isKeyEnabled"] is True
    assert red["tokenCount"] is None
    # The input is never mutated.
    assert body["password"] == PASSWORD


def test_redact_headers() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signatureXk9Q"
    red = redact_headers(
        {
            "Authorization": f"Bearer {token}",
            "x-api-version": "1.3-rev2",
            "Cookie": "pulse_session=abc",
            "X-Api-Key": "k",
        }
    )
    assert red == {
        "Authorization": "Bearer eyJhb…Xk9Q",
        "x-api-version": "1.3-rev2",
        "Cookie": MASK,
        "X-Api-Key": MASK,
    }


def test_redact_url() -> None:
    url = "https://user:pw@vbr.test/api/v1/jobs?nameFilter=SQL*&access_token=abc&limit=25"
    assert redact_url(url) == (
        f"https://vbr.test/api/v1/jobs?nameFilter=SQL*&access_token={MASK}&limit=25"
    )


async def test_no_secret_reaches_the_bus(respx_mock: respx.MockRouter) -> None:
    """End to end: log in, refresh, call, log out — then search every event for secrets."""
    bus = InspectorBus()
    clock = FakeClock()
    first, second = token_body(1), token_body(2)
    respx_mock.post("https://vbr.test/api/oauth2/token").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    respx_mock.get("https://vbr.test/api/v1/serverTime").respond(200, json={})
    respx_mock.post("https://vbr.test/api/oauth2/logout").respond(200)

    from pydantic import SecretStr

    from pulse.vbr.client import Credentials

    client = VbrClient(
        "https://vbr.test",
        lambda: Credentials("svc-pulse-ops", SecretStr(PASSWORD)),
        inspector=bus,
        clock=clock,
    )
    await client.request("GetServerTime")
    clock.advance(800)
    await client.request("GetServerTime")
    await client.aclose()

    dump = "\n".join(e.to_json() for e in bus.recent())
    secrets = [
        PASSWORD,
        str(first["access_token"]),
        str(first["refresh_token"]),
        str(second["access_token"]),
        str(second["refresh_token"]),
    ]
    for secret in secrets:
        assert secret not in dump
    assert len(bus.recent()) == 5  # token, time, refresh, time, logout
    login = bus.recent()[0]
    assert login.request_body["password"] == MASK
    assert login.response_body["access_token"].startswith("eyJhb…")

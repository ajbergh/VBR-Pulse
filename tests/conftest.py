from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from pulse.inspector.bus import InspectorBus
from pulse.vbr.client import Credentials, VbrClient

BASE = "https://vbr.test"
PASSWORD = "Sup3r-Secret-P@ss"


def token_body(n: int = 1, expires_in: int = 900) -> dict[str, Any]:
    """A token response whose tokens are long enough to exercise masking."""
    return {
        "access_token": f"eyJhbGciOiJIUzI1NiJ9.access-{n}-" + "a" * 40 + f"-END{n}",
        "token_type": "bearer",
        "refresh_token": f"refresh-{n}-" + "r" * 40 + f"-RFN{n}",
        "expires_in": expires_in,
        ".issued": "2026-09-30T08:00:00Z",
        ".expires": "2026-09-30T08:15:00Z",
    }


def access_token(n: int) -> str:
    return str(token_body(n)["access_token"])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def bus() -> InspectorBus:
    return InspectorBus()


@pytest.fixture
def vbr() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(
    clock: FakeClock, sleeps: list[float], bus: InspectorBus
) -> AsyncIterator[VbrClient]:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    c = VbrClient(
        BASE,
        lambda: Credentials("svc-pulse-ops", SecretStr(PASSWORD)),
        inspector=bus,
        clock=clock,
        sleep=fake_sleep,
    )
    yield c
    await c._http.aclose()


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))

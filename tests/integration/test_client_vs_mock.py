"""Phase 2 acceptance: the Phase 1 client behaviours hold against the mock app, not just respx."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from pulse.inspector.bus import InspectorBus
from pulse.mock.app import MOCK_BASE_URL, mock_transport
from pulse.mock.state import MockVbr
from pulse.vbr.client import Credentials, VbrClient
from pulse.vbr.errors import (
    VbrBadRequest,
    VbrForbidden,
    VbrNetworkError,
    VbrNotFound,
    VbrRequestInvalid,
    VbrServerError,
    VbrUnauthorized,
)
from tests.conftest import FakeClock

SQL_DAILY = "SQL Daily"


@pytest.fixture
def mock(clock: FakeClock) -> MockVbr:
    return MockVbr("happy", clock=clock)


def make_client(
    mock: MockVbr,
    clock: FakeClock,
    bus: InspectorBus,
    sleeps: list[float],
    user: str = "svc-pulse-ops",
    api_version: str = "1.3-rev2",
) -> VbrClient:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return VbrClient(
        MOCK_BASE_URL,
        lambda: Credentials(user, SecretStr("mock")),
        api_version=api_version,
        inspector=bus,
        mode="mock",
        transport=mock_transport(mock),
        clock=clock,
        sleep=fake_sleep,
    )


@pytest.fixture
async def client(
    mock: MockVbr, clock: FakeClock, bus: InspectorBus, sleeps: list[float]
) -> AsyncIterator[VbrClient]:
    c = make_client(mock, clock, bus, sleeps)
    yield c
    await c.aclose()


async def job_id(client: VbrClient, name: str = SQL_DAILY) -> str:
    states = await client.request("GetAllJobsStates", params={"nameFilter": name})
    return str(states["data"][0]["id"])


async def test_login_and_logout(client: VbrClient, mock: MockVbr) -> None:
    await client.login()
    assert client.is_authenticated
    await client.logout()
    assert mock.calls["Logout"] == 1
    assert not mock._access  # token revoked server-side


async def test_x_api_version_required_on_every_request(
    mock: MockVbr, clock: FakeClock, bus: InspectorBus, sleeps: list[float]
) -> None:
    good = make_client(mock, clock, bus, sleeps)
    await good.request("GetServerTime")  # the mock rejects any call without the header
    await good.aclose()

    bad = make_client(mock, clock, bus, sleeps, api_version="1.2-rev1")
    with pytest.raises(VbrBadRequest, match="x-api-version"):
        await bad.login()
    await bad.aclose()


async def test_proactive_refresh(client: VbrClient, mock: MockVbr, clock: FakeClock) -> None:
    await client.request("GetServerTime")
    clock.advance(720)
    await client.request("GetServerTime")
    assert mock.calls["CreateToken"] == 2
    assert mock.calls["GetServerTime"] == 2  # no 401 on the way


async def test_reactive_refresh_after_server_side_expiry(
    client: VbrClient, mock: MockVbr, clock: FakeClock, bus: InspectorBus
) -> None:
    mock.set_scenario("token-expiry")
    await client.request("GetServerTime")
    clock.advance(61)

    await client.request("GetServerTime")

    statuses = [(e.operation_id, e.status) for e in bus.recent()]
    assert statuses == [
        ("CreateToken", 200),
        ("GetServerTime", 200),
        ("GetServerTime", 401),
        ("CreateToken", 200),
        ("GetServerTime", 200),
    ]
    assert bus.recent()[2].error_code == "ExpiredToken"


async def test_refresh_tokens_are_single_use(mock: MockVbr) -> None:
    first = mock.create_token(
        {"grant_type": "password", "username": "svc-pulse-ops", "password": "x"}
    )
    form = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}
    mock.create_token(form)
    with pytest.raises(Exception, match="refresh token"):
        mock.create_token(form)


async def test_unknown_account_rejected(
    mock: MockVbr, clock: FakeClock, bus: InspectorBus, sleeps: list[float]
) -> None:
    c = make_client(mock, clock, bus, sleeps, user="intruder")
    with pytest.raises(VbrUnauthorized):
        await c.login()
    await c.aclose()


async def test_post_not_retried_on_500(client: VbrClient, mock: MockVbr) -> None:
    jid = await job_id(client)
    mock.inject_fault("StartJob", 500)
    with pytest.raises(VbrServerError):
        await client.request("StartJob", path_params={"id": jid}, json={"performActiveFull": False})
    assert mock.calls["StartJob"] == 1


async def test_post_not_retried_on_dropped_connection(client: VbrClient, mock: MockVbr) -> None:
    jid = await job_id(client)
    mock.inject_fault("StartJob", 0)
    with pytest.raises(VbrNetworkError):
        await client.request("StartJob", path_params={"id": jid}, json={"performActiveFull": False})
    assert mock.calls["StartJob"] == 1


async def test_get_retried_on_503(client: VbrClient, mock: MockVbr, sleeps: list[float]) -> None:
    mock.inject_fault("GetAllJobsStates", 503, times=2)
    result = await client.request("GetAllJobsStates")
    assert result["pagination"]["total"] == 42
    assert mock.calls["GetAllJobsStates"] == 3
    assert sleeps == [1.0, 2.0]


async def test_paging_across_three_pages(client: VbrClient, mock: MockVbr) -> None:
    names = [j["name"] async for j in client.paginate("GetAllJobsStates", limit=20)]
    assert len(names) == 42
    assert len(set(names)) == 42
    assert mock.calls["GetAllJobsStates"] == 3


async def test_forbidden_for_viewer(
    mock: MockVbr, clock: FakeClock, bus: InspectorBus, sleeps: list[float]
) -> None:
    viewer = make_client(mock, clock, bus, sleeps, user="svc-pulse-view")
    jid = await job_id(viewer)
    with pytest.raises(VbrForbidden) as err:
        await viewer.request("StartJob", path_params={"id": jid}, json={"performActiveFull": False})
    assert err.value.error_code == "AccessDenied"
    await viewer.aclose()


async def test_not_found(client: VbrClient) -> None:
    with pytest.raises(VbrNotFound) as err:
        await client.request(
            "GetSession", path_params={"id": "5d1b5f02-0000-0000-0000-000000000000"}
        )
    assert err.value.resource_id == "5d1b5f02-0000-0000-0000-000000000000"


async def test_bad_request_when_already_running(client: VbrClient) -> None:
    jid = await job_id(client)
    await client.request("StartJob", path_params={"id": jid}, json={"performActiveFull": False})
    with pytest.raises(VbrBadRequest, match="already running"):
        await client.request("StartJob", path_params={"id": jid}, json={"performActiveFull": False})


async def test_client_side_body_validation_still_applies(client: VbrClient, mock: MockVbr) -> None:
    with pytest.raises(VbrRequestInvalid):
        await client.request("StartMalwareBackupScan", json={"type": "Backup"})
    assert mock.calls["StartMalwareBackupScan"] == 0


async def test_no_secret_reaches_the_bus(
    client: VbrClient, mock: MockVbr, clock: FakeClock, bus: InspectorBus
) -> None:
    issued: list[str] = []
    create_token = mock.create_token

    def spy(form: dict[str, str]) -> dict[str, Any]:
        tokens = create_token(form)
        issued.extend([tokens["access_token"], tokens["refresh_token"]])
        return tokens

    mock.create_token = spy  # type: ignore[method-assign]
    await client.request("GetServerTime")
    clock.advance(800)
    await client.request("GetServerTime")
    await client.logout()

    dump = "\n".join(e.to_json() for e in bus.recent())
    assert len(issued) == 4
    for token in issued:
        assert token not in dump
    assert "eyJhb…" in dump
    assert '"password":"••••••"' in dump

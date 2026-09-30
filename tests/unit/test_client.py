"""Phase 1 acceptance: VbrClient auth, refresh, retry, paging, errors (PLAN §4.4, §5.3)."""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
import respx

from pulse.inspector.bus import InspectorBus
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import (
    VbrBadRequest,
    VbrForbidden,
    VbrNetworkError,
    VbrNotFound,
    VbrRequestInvalid,
    VbrServerError,
    VbrTlsError,
    VbrUnauthorized,
)
from tests.conftest import PASSWORD, FakeClock, access_token, form, token_body

JOB_ID = "3c5557b1-71e8-4508-8dce-4e743b294ef5"
SESSION = {"id": "5d1b5f02-a4a9-4483-8142-d2540ad39c85", "state": "Starting"}


def ok_token(vbr: respx.MockRouter, *bodies: dict[str, object]) -> respx.Route:
    route = vbr.post("/api/oauth2/token")
    route.side_effect = [httpx.Response(200, json=b) for b in bodies or (token_body(1),)]
    return route


async def test_login_success(client: VbrClient, vbr: respx.MockRouter) -> None:
    token = ok_token(vbr)

    await client.login()

    assert client.is_authenticated
    sent = form(token.calls.last.request)
    assert sent == {"grant_type": "password", "username": "svc-pulse-ops", "password": PASSWORD}
    assert token.calls.last.request.headers["content-type"] == "application/x-www-form-urlencoded"
    info = client.token_info()
    assert info is not None
    assert info.lifetime_seconds == 900
    assert info.refresh_in_seconds == pytest.approx(720)


async def test_x_api_version_on_every_request_including_token(
    client: VbrClient, vbr: respx.MockRouter
) -> None:
    ok_token(vbr)
    vbr.get("/api/v1/serverInfo").respond(200, json={"name": "vbr01"})
    vbr.post(f"/api/v1/jobs/{JOB_ID}/start").respond(201, json=SESSION)
    vbr.post("/api/oauth2/logout").respond(200)

    await client.request("GetServerInfo")
    await client.request("StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": False})
    await client.logout()

    assert len(vbr.calls) == 4
    for call in vbr.calls:
        assert call.request.headers["x-api-version"] == "1.3-rev2"


async def test_bearer_token_sent(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    route = vbr.get("/api/v1/serverTime").respond(200, json={})

    await client.request("GetServerTime")

    assert route.calls.last.request.headers["authorization"] == f"Bearer {access_token(1)}"


async def test_proactive_refresh_at_80_percent(
    client: VbrClient, vbr: respx.MockRouter, clock: FakeClock
) -> None:
    token = ok_token(vbr, token_body(1), token_body(2))
    route = vbr.get("/api/v1/serverTime").respond(200, json={})

    await client.request("GetServerTime")
    clock.advance(719)  # 900 s * 0.8 = 720 s
    await client.request("GetServerTime")
    assert token.call_count == 1

    clock.advance(1)
    await client.request("GetServerTime")

    assert token.call_count == 2
    assert form(token.calls.last.request) == {
        "grant_type": "refresh_token",
        "refresh_token": str(token_body(1)["refresh_token"]),
    }
    assert route.calls.last.request.headers["authorization"] == f"Bearer {access_token(2)}"


async def test_single_retry_after_401(client: VbrClient, vbr: respx.MockRouter) -> None:
    token = ok_token(vbr, token_body(1), token_body(2))
    route = vbr.get("/api/v1/jobs/states")
    route.side_effect = [
        httpx.Response(401, json={"errorCode": "ExpiredToken", "message": "Token expired"}),
        httpx.Response(200, json={"data": [], "pagination": {"total": 0, "count": 0}}),
    ]

    result = await client.request("GetAllJobsStates")

    assert result["pagination"]["total"] == 0
    assert token.call_count == 2
    assert form(token.calls.last.request)["grant_type"] == "refresh_token"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {access_token(2)}"


async def test_second_401_is_raised(client: VbrClient, vbr: respx.MockRouter) -> None:
    token = ok_token(vbr, token_body(1), token_body(2))
    route = vbr.get("/api/v1/jobs/states").respond(
        401, json={"errorCode": "InvalidToken", "message": "Invalid token"}
    )

    with pytest.raises(VbrUnauthorized) as err:
        await client.request("GetAllJobsStates")

    assert err.value.error_code == "InvalidToken"
    assert route.call_count == 2
    assert token.call_count == 2


async def test_failed_refresh_falls_back_to_password_login(
    client: VbrClient, vbr: respx.MockRouter, clock: FakeClock
) -> None:
    token = vbr.post("/api/oauth2/token")
    token.side_effect = [
        httpx.Response(200, json=token_body(1)),
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(200, json=token_body(2)),
    ]
    vbr.get("/api/v1/serverTime").respond(200, json={})

    await client.request("GetServerTime")
    clock.advance(800)
    await client.request("GetServerTime")

    grants = [form(c.request)["grant_type"] for c in token.calls]
    assert grants == ["password", "refresh_token", "password"]


async def test_concurrent_requests_share_one_refresh(
    client: VbrClient, vbr: respx.MockRouter, clock: FakeClock
) -> None:
    token = ok_token(vbr, token_body(1), token_body(2))
    vbr.get("/api/v1/serverTime").respond(200, json={})
    await client.login()
    clock.advance(800)

    await asyncio.gather(*(client.request("GetServerTime") for _ in range(5)))

    assert token.call_count == 2


async def test_post_not_retried_on_500(
    client: VbrClient, vbr: respx.MockRouter, sleeps: list[float]
) -> None:
    ok_token(vbr)
    route = vbr.post(f"/api/v1/jobs/{JOB_ID}/start").respond(
        500, json={"errorCode": "UnknownError", "message": "Boom"}
    )

    with pytest.raises(VbrServerError) as err:
        await client.request(
            "StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": False}
        )

    assert route.call_count == 1
    assert sleeps == []
    assert err.value.status == 500
    assert err.value.error_code == "UnknownError"


async def test_post_not_retried_on_network_error(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    route = vbr.post(f"/api/v1/jobs/{JOB_ID}/stop").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(VbrNetworkError):
        await client.request("StopJob", path_params={"id": JOB_ID})

    assert route.call_count == 1


async def test_get_retried_on_5xx_with_backoff(
    client: VbrClient, vbr: respx.MockRouter, sleeps: list[float]
) -> None:
    ok_token(vbr)
    route = vbr.get("/api/v1/serverTime")
    route.side_effect = [
        httpx.Response(503, json={"errorCode": "ServiceUnavailable", "message": "busy"}),
        httpx.ConnectError("reset"),
        httpx.Response(200, json={"serverTime": "2026-09-30T08:00:00Z"}),
    ]

    result = await client.request("GetServerTime")

    assert result == {"serverTime": "2026-09-30T08:00:00Z"}
    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]


async def test_get_gives_up_after_three_attempts(
    client: VbrClient, vbr: respx.MockRouter, sleeps: list[float]
) -> None:
    ok_token(vbr)
    route = vbr.get("/api/v1/serverTime").respond(502)

    with pytest.raises(VbrServerError):
        await client.request("GetServerTime")

    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]


async def test_paging_across_three_pages(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    jobs = [{"id": f"job-{i}", "name": f"Job {i}"} for i in range(5)]

    def page(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        limit = int(request.url.params["limit"])
        chunk = jobs[skip : skip + limit]
        return httpx.Response(
            200,
            json={
                "data": chunk,
                "pagination": {
                    "total": len(jobs),
                    "count": len(chunk),
                    "skip": skip,
                    "limit": limit,
                },
            },
        )

    route = vbr.get("/api/v1/jobs/states").mock(side_effect=page)

    items = [
        item
        async for item in client.paginate(
            "GetAllJobsStates", params={"nameFilter": "Job*"}, limit=2
        )
    ]

    assert [i["id"] for i in items] == [j["id"] for j in jobs]
    assert route.call_count == 3
    assert [c.request.url.params["skip"] for c in route.calls] == ["0", "2", "4"]
    assert all(c.request.url.params["nameFilter"] == "Job*" for c in route.calls)


async def test_paging_stops_on_empty_page(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    route = vbr.get("/api/v1/sessions").respond(
        200, json={"data": [], "pagination": {"total": 99, "count": 0}}
    )

    items = [item async for item in client.paginate("GetAllSessions")]

    assert items == []
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "exc", "code"),
    [
        (400, VbrBadRequest, "UnexpectedContent"),
        (403, VbrForbidden, "AccessDenied"),
        (404, VbrNotFound, "NotFound"),
    ],
)
async def test_error_mapping(
    client: VbrClient,
    vbr: respx.MockRouter,
    status: int,
    exc: type[Exception],
    code: str,
) -> None:
    ok_token(vbr)
    vbr.post(f"/api/v1/jobs/{JOB_ID}/start").respond(
        status, json={"errorCode": code, "message": "Nope", "resourceId": JOB_ID}
    )

    with pytest.raises(exc) as err:
        await client.request(
            "StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": False}
        )

    assert err.value.error_code == code  # type: ignore[attr-defined]
    assert err.value.resource_id == JOB_ID  # type: ignore[attr-defined]
    assert err.value.operation_id == "StartJob"  # type: ignore[attr-defined]


async def test_login_failure_raises_unauthorized(client: VbrClient, vbr: respx.MockRouter) -> None:
    vbr.post("/api/oauth2/token").respond(
        401, json={"errorCode": "AccessDenied", "message": "Invalid credentials"}
    )

    with pytest.raises(VbrUnauthorized):
        await client.login()
    assert not client.is_authenticated


async def test_request_body_validated_against_spec(
    client: VbrClient, vbr: respx.MockRouter
) -> None:
    ok_token(vbr)
    route = vbr.post(f"/api/v1/jobs/{JOB_ID}/start")

    with pytest.raises(VbrRequestInvalid, match="performActiveFull"):
        await client.request(
            "StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": "yes"}
        )
    with pytest.raises(VbrRequestInvalid):
        await client.request("StartJob", path_params={"id": JOB_ID}, json={})

    assert route.call_count == 0


async def test_unknown_operation_id(client: VbrClient) -> None:
    with pytest.raises(KeyError, match="NotARealOperation"):
        await client.request("NotARealOperation")


async def test_missing_path_param(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    with pytest.raises(ValueError, match="missing path parameters"):
        await client.request("GetSession")


async def test_query_params_encoded(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    route = vbr.get("/api/v1/backupInfrastructure/repositories/states").respond(
        200, json={"data": [], "pagination": {"total": 0, "count": 0}}
    )

    await client.request(
        "GetAllRepositoriesStates",
        params={"orderColumn": "Name", "orderAsc": True, "nameFilter": None},
    )

    params = route.calls.last.request.url.params
    assert params["orderColumn"] == "Name"
    assert params["orderAsc"] == "true"
    assert "nameFilter" not in params


async def test_logout_clears_tokens(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    route = vbr.post("/api/oauth2/logout").respond(200)
    await client.login()

    await client.logout()

    assert not client.is_authenticated
    assert route.calls.last.request.headers["authorization"] == f"Bearer {access_token(1)}"


async def test_logout_never_raises(client: VbrClient, vbr: respx.MockRouter) -> None:
    ok_token(vbr)
    vbr.post("/api/oauth2/logout").mock(side_effect=httpx.ConnectError("gone"))
    await client.login()

    await client.logout()
    await client.logout()  # second call is a no-op

    assert not client.is_authenticated


async def test_network_error_message(client: VbrClient, vbr: respx.MockRouter) -> None:
    vbr.post("/api/oauth2/token").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(VbrNetworkError) as err:
        await client.login()

    assert str(err.value) == "Can't reach vbr.test on port 443. Check the address or VPN."


async def test_tls_error_message(client: VbrClient, vbr: respx.MockRouter) -> None:
    cause = ssl.SSLCertVerificationError("certificate verify failed")
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")
    exc.__cause__ = cause
    vbr.post("/api/oauth2/token").mock(side_effect=exc)

    with pytest.raises(VbrTlsError, match="PULSE_CA_BUNDLE"):
        await client.login()


def test_base_url_normalised() -> None:
    c = VbrClient("https://vbr01.lab.local/api/", lambda: None)  # type: ignore[arg-type,return-value]
    assert c.base_url == "https://vbr01.lab.local"


async def test_inspector_event_per_call(
    client: VbrClient, vbr: respx.MockRouter, bus: InspectorBus
) -> None:
    ok_token(vbr)
    vbr.post(f"/api/v1/jobs/{JOB_ID}/start").respond(201, json=SESSION)
    vbr.get(f"/api/v1/sessions/{SESSION['id']}").respond(200, json=SESSION)

    await client.request("StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": False})
    await client.request(
        "GetSession", path_params={"id": SESSION["id"]}, group=f"session:{SESSION['id']}"
    )

    token_evt, start_evt, poll_evt = bus.recent()
    assert token_evt.operation_id == "CreateToken"
    assert start_evt.operation_id == "StartJob"
    assert start_evt.method == "POST"
    assert start_evt.status == 201
    assert start_evt.url == f"https://vbr.test/api/v1/jobs/{JOB_ID}/start"
    assert start_evt.request_body == {"performActiveFull": False}
    assert start_evt.response_body == SESSION
    assert start_evt.docs_url == (
        "https://helpcenter.veeam.com/references/vbr/13/rest/1.3-rev2/tag/Jobs#operation/StartJob"
    )
    assert start_evt.request_headers["x-api-version"] == "1.3-rev2"
    assert start_evt.mode == "live"
    assert poll_evt.group == f"session:{SESSION['id']}"


async def test_inspector_records_error_code(
    client: VbrClient, vbr: respx.MockRouter, bus: InspectorBus
) -> None:
    ok_token(vbr)
    vbr.post(f"/api/v1/jobs/{JOB_ID}/start").respond(
        403, json={"errorCode": "AccessDenied", "message": "Forbidden"}
    )

    with pytest.raises(VbrForbidden):
        await client.request(
            "StartJob", path_params={"id": JOB_ID}, json={"performActiveFull": False}
        )

    evt = bus.recent()[-1]
    assert evt.status == 403
    assert evt.error_code == "AccessDenied"


async def test_inspector_records_network_failures(
    client: VbrClient, vbr: respx.MockRouter, bus: InspectorBus
) -> None:
    vbr.post("/api/oauth2/token").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(VbrNetworkError):
        await client.login()

    evt = bus.recent()[-1]
    assert evt.status is None
    assert evt.error_code == "NetworkError"
    assert evt.error_message is not None

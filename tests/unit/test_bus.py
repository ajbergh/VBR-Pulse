from __future__ import annotations

import json

from pulse.inspector.bus import InspectorBus, InspectorEvent


def event(n: int) -> InspectorEvent:
    return InspectorEvent(
        operation_id="GetServerTime",
        docs_url="https://example.invalid",
        method="GET",
        url=f"https://vbr.test/api/v1/serverTime?n={n}",
        status=200,
    )


def test_ring_buffer_keeps_last_n() -> None:
    bus = InspectorBus(capacity=3)
    for n in range(5):
        bus.publish(event(n))
    assert [e.url[-1] for e in bus.recent()] == ["2", "3", "4"]


async def test_subscribers_receive_new_events() -> None:
    bus = InspectorBus()
    bus.publish(event(0))
    async with bus.subscribe() as queue:
        assert bus.subscriber_count == 1
        bus.publish(event(1))
        received = await queue.get()
    assert received.url.endswith("n=1")
    assert bus.subscriber_count == 0


def test_event_serialises_with_appendix_c_keys() -> None:
    data = json.loads(event(0).to_json())
    for key in (
        "id",
        "ts",
        "operationId",
        "docsUrl",
        "method",
        "url",
        "requestHeaders",
        "requestBody",
        "status",
        "durationMs",
        "responseBody",
        "errorCode",
        "group",
        "mode",
    ):
        assert key in data
    assert data["id"].startswith("evt_")

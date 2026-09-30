"""Inspector bus: an in-memory ring buffer of redacted VBR calls with SSE fan-out.

Event schema: PLAN Appendix C. Callers must redact before publishing; the
VbrClient does this for every request it sends.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Mode = Literal["live", "mock"]

DEFAULT_CAPACITY = 200
_SUBSCRIBER_QUEUE = 500


def new_event_id() -> str:
    """Time-sortable id: `evt_` + 12 hex digits of ms since epoch + 8 random hex digits."""
    return f"evt_{time.time_ns() // 1_000_000:012x}{os.urandom(4).hex()}"


class InspectorEvent(BaseModel):
    """One HTTP exchange with VBR, already redacted. Serialises with camelCase keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    id: str = Field(default_factory=new_event_id)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    operation_id: str
    docs_url: str
    method: str
    url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    status: int | None = None
    duration_ms: int = 0
    response_body: Any = None
    response_truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    group: str | None = None
    mode: Mode = "live"

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)


class InspectorBus:
    """Keeps the last `capacity` events and broadcasts new ones to subscribers."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._events: deque[InspectorEvent] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue[InspectorEvent]] = set()

    def publish(self, event: InspectorEvent) -> None:
        self._events.append(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled browser tab must never block VBR calls; it can reload.
                pass

    def recent(self) -> list[InspectorEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[InspectorEvent]]:
        queue: asyncio.Queue[InspectorEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

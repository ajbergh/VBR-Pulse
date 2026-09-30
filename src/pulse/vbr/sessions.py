"""SessionTracker: poll VBR sessions and fan updates out to subscribers (PLAN §4.4).

- Polls `GetSession` every `poll_seconds`, backing off to 15 s after 2 minutes and 30 s
  after 10 minutes. Ends when the session state is `Stopped`.
- On a Failed result, fetches `GetSessionLogs` once so the UI can show why.
- One poller per session id, however many browser tabs watch it.
- On a network or 5xx error (after the client's own GET retries) it reports
  "connection lost" and tries again after 8 s, or sooner via `retry_now()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError, VbrServerError

log = logging.getLogger(__name__)

BACKOFF_AFTER = ((600.0, 30.0), (120.0, 15.0))  # (elapsed seconds, minimum interval)
RECONNECT_SECONDS = 8.0
FAILED_LOG_LINES = 20
_KEEP_FINISHED = 50

FinishHook = Callable[["SessionSnapshot"], Awaitable[None]]


@dataclass(frozen=True)
class SessionSnapshot:
    """What the UI needs to draw a session track. Built only from API responses."""

    id: str
    name: str
    job_id: str
    session_type: str
    state: str
    progress: int
    result: str
    message: str
    is_canceled: bool
    creation_time: str | None
    end_time: str | None
    poll_seconds: float
    polls: int = 0
    logs: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    retry_in: float | None = None
    ended: bool = False  # no more updates will follow
    history: tuple[str, ...] = field(default_factory=tuple)  # states seen, in order

    @property
    def stopped(self) -> bool:
        return self.state == "Stopped"

    @classmethod
    def from_body(
        cls, body: dict[str, Any], poll_seconds: float, previous: SessionSnapshot | None = None
    ) -> SessionSnapshot:
        result = body.get("result") or {}
        state = body.get("state", "Starting")
        history = previous.history if previous else ()
        if not history or history[-1] != state:
            history = (*history, state)
        return cls(
            id=body["id"],
            name=body.get("name", ""),
            job_id=body.get("jobId", ""),
            session_type=body.get("sessionType", ""),
            state=state,
            progress=int(body.get("progressPercent") or 0),
            result=result.get("result", "None"),
            message=result.get("message", ""),
            is_canceled=bool(result.get("isCanceled", False)),
            creation_time=body.get("creationTime"),
            end_time=body.get("endTime"),
            poll_seconds=poll_seconds,
            polls=previous.polls if previous else 0,
            logs=previous.logs if previous else (),
            history=history,
            ended=state == "Stopped",
        )


def poll_interval(base: float, elapsed: float) -> float:
    for after, minimum in BACKOFF_AFTER:
        if elapsed >= after:
            return max(base, minimum)
    return base


class SessionTracker:
    def __init__(
        self,
        client: VbrClient,
        *,
        poll_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._latest: OrderedDict[str, SessionSnapshot] = OrderedDict()
        self._subscribers: dict[str, set[asyncio.Queue[SessionSnapshot]]] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._hooks: dict[str, FinishHook] = {}

    # ------------------------------------------------------------------ public

    def track(self, body: dict[str, Any], on_finish: FinishHook | None = None) -> SessionSnapshot:
        """Start watching the session in `body` (e.g. a StartJob response). Idempotent."""
        session_id = body["id"]
        if on_finish is not None:
            self._hooks.setdefault(session_id, on_finish)
        if session_id not in self._latest:
            self._remember(SessionSnapshot.from_body(body, self.poll_seconds))
        task = self._tasks.get(session_id)
        if (task is None or task.done()) and not self._latest[session_id].ended:
            self._tasks[session_id] = asyncio.create_task(
                self._poll(session_id), name=f"session-poller-{session_id}"
            )
        return self._latest[session_id]

    def latest(self, session_id: str) -> SessionSnapshot | None:
        return self._latest.get(session_id)

    def running(self) -> dict[str, SessionSnapshot]:
        """Latest snapshot of each session that hasn't ended, keyed by job id."""
        return {s.job_id: s for s in self._latest.values() if not s.ended and s.job_id}

    def active_pollers(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done())

    def retry_now(self, session_id: str) -> None:
        if event := self._wake.get(session_id):
            event.set()

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[asyncio.Queue[SessionSnapshot]]:
        """Queue of snapshots for one session; starts with the latest one if known."""
        queue: asyncio.Queue[SessionSnapshot] = asyncio.Queue()
        if snapshot := self._latest.get(session_id):
            queue.put_nowait(snapshot)
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            self._subscribers.get(session_id, set()).discard(queue)

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # ------------------------------------------------------------------ internals

    def _remember(self, snapshot: SessionSnapshot) -> None:
        self._latest[snapshot.id] = snapshot
        self._latest.move_to_end(snapshot.id)
        while len(self._latest) > _KEEP_FINISHED:
            oldest = next(iter(self._latest))
            if oldest in self._tasks and not self._tasks[oldest].done():
                break
            self._latest.pop(oldest)

    def _publish(self, snapshot: SessionSnapshot) -> None:
        self._remember(snapshot)
        for queue in self._subscribers.get(snapshot.id, set()):
            queue.put_nowait(snapshot)

    async def _nap(self, session_id: str, seconds: float) -> None:
        """Sleep, but wake early if retry_now() is called."""
        wake = self._wake.setdefault(session_id, asyncio.Event())
        wake.clear()
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waiter = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (sleeper, waiter):
                pending.cancel()

    async def _poll(self, session_id: str) -> None:
        started = self._clock()
        group = f"session:{session_id}"
        while True:
            interval = poll_interval(self.poll_seconds, self._clock() - started)
            previous = self._latest[session_id]
            try:
                body = await self.client.request(
                    "GetSession", path_params={"id": session_id}, group=group
                )
            except VbrServerError as exc:
                self._publish(replace(previous, error=exc.message, retry_in=RECONNECT_SECONDS))
                await self._nap(session_id, RECONNECT_SECONDS)
                continue
            except VbrError as exc:  # 401 after refresh, 403, 404: nothing more to poll
                self._publish(replace(previous, error=exc.message, retry_in=None, ended=True))
                await self._finish(session_id)
                return

            snapshot = SessionSnapshot.from_body(body, interval, previous)
            snapshot = replace(snapshot, polls=previous.polls + 1)
            if snapshot.stopped and snapshot.result == "Failed":
                snapshot = replace(snapshot, logs=await self._failure_logs(session_id))
            if snapshot.stopped:
                self._remember(snapshot)
                await self._finish(session_id)
                self._publish(snapshot)
                return
            self._publish(snapshot)
            await self._nap(session_id, interval)

    async def _failure_logs(self, session_id: str) -> tuple[dict[str, Any], ...]:
        try:
            logs = await self.client.request("GetSessionLogs", path_params={"id": session_id})
        except VbrError:
            return ()
        records = (logs.get("records") or []) if isinstance(logs, dict) else []
        return tuple(records[-FAILED_LOG_LINES:])

    async def _finish(self, session_id: str) -> None:
        hook = self._hooks.pop(session_id, None)
        if hook is not None:
            try:
                await hook(self._latest[session_id])
            except Exception:
                log.exception("Session finish hook failed for %s", session_id)
        self._wake.pop(session_id, None)

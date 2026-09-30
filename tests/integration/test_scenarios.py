"""Phase 2 acceptance: all six scenarios run end to end; every mock response matches the spec."""

from __future__ import annotations

from typing import Any

import pytest

from pulse.mock.runner import run_scenario
from pulse.mock.scenarios import SCENARIO_KEYS
from tests.conftest import FakeClock
from tests.spec_transport import SpecCheckingTransport


@pytest.mark.parametrize("key", SCENARIO_KEYS)
async def test_scenario_passes_with_spec_valid_responses(key: str) -> None:
    clock = FakeClock()
    checkers: list[SpecCheckingTransport] = []

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    def wrap(inner: Any) -> SpecCheckingTransport:
        checkers.append(SpecCheckingTransport(inner))
        return checkers[-1]

    report = await run_scenario(key, clock=clock, sleep=sleep, transport_wrapper=wrap)

    assert report.passed, f"{report.error}\n" + "\n".join(report.steps)
    assert sum(checkers[0].checked.values()) == len(report.events)
    assert all(event.mode == "mock" for event in report.events)


async def test_happy_polls_are_grouped_by_session() -> None:
    clock = FakeClock()

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    report = await run_scenario("happy", clock=clock, sleep=sleep)

    polls = [e for e in report.events if e.operation_id == "GetSession"]
    assert len(polls) == 9  # 0, 5, … 40 s
    assert len({e.group for e in polls}) == 1
    assert polls[0].group is not None and polls[0].group.startswith("session:")
    assert [e.operation_id for e in report.events[:2]] == ["CreateToken", "GetServerInfo"]
    assert report.events[-1].operation_id == "Logout"

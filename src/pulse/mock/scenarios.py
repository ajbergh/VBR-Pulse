"""Scripted mock scenarios (PLAN §6.2) and session-timeline evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SessionKind = Literal["job", "quickBackup", "malwareScan"]
NO_RESULT = {"result": "None", "message": "", "isCanceled": False}


@dataclass(frozen=True)
class Step:
    after: float
    body: dict[str, Any]


@dataclass(frozen=True)
class LogLine:
    after: float
    status: str
    title: str


@dataclass(frozen=True)
class Timeline:
    key: str
    steps: tuple[Step, ...]
    logs: tuple[LogLine, ...]

    @property
    def duration(self) -> float:
        return self.steps[-1].after

    def at(self, elapsed: float) -> dict[str, Any]:
        """State, progressPercent and result `elapsed` seconds after the session started."""
        current = self.steps[0]
        following: Step | None = None
        for i, step in enumerate(self.steps):
            if step.after <= elapsed:
                current = step
                following = self.steps[i + 1] if i + 1 < len(self.steps) else None
        view = {"result": NO_RESULT, **current.body}
        if (
            following is not None
            and current.body.get("state") == "Working"
            and following.body.get("state") == "Working"
        ):
            start, end = current.body["progressPercent"], following.body["progressPercent"]
            share = (elapsed - current.after) / (following.after - current.after)
            view["progressPercent"] = int(start + (end - start) * share)
        return view

    def logs_until(self, elapsed: float) -> list[LogLine]:
        return [line for line in self.logs if line.after <= elapsed]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    timelines: dict[str, Timeline]
    access_token_lifetime: float | None = None
    force_role: str | None = None
    seed_malware_events: bool = False

    def timeline(self, kind: SessionKind) -> Timeline:
        return self.timelines[kind]


@cache
def load_scenarios() -> dict[str, Scenario]:
    raw = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
    timelines = {
        key: Timeline(
            key=key,
            steps=tuple(Step(s["afterSeconds"], s["body"]) for s in steps),
            logs=tuple(
                LogLine(line["afterSeconds"], line["status"], line["title"])
                for line in raw["logs"].get(key, [])
            ),
        )
        for key, steps in raw["timelines"].items()
    }
    return {
        key: Scenario(
            key=key,
            title=spec["title"],
            description=spec["description"],
            timelines={kind: timelines[name] for kind, name in spec["timelines"].items()},
            access_token_lifetime=spec.get("accessTokenLifetimeSeconds"),
            force_role=spec.get("forceRole"),
            seed_malware_events=spec.get("seedMalwareEvents", False),
        )
        for key, spec in raw["scenarios"].items()
    }


SCENARIO_KEYS = ("happy", "warning", "failed", "token-expiry", "forbidden", "incident")

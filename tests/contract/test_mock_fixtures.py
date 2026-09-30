"""Every mock fixture validates against the 1.3-rev2 spec (PLAN §6.1), so fixtures can't drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pulse.mock.scenarios import FIXTURES, SCENARIO_KEYS, load_scenarios
from pulse.vbr.operations import OPERATIONS
from pulse.vbr.spec import validate_response_body, validate_schema


def _fixture_entries() -> list[tuple[str, dict[str, Any]]]:
    entries = []
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name.startswith("_") or path.name == "scenarios.json":
            continue
        content = json.loads(path.read_text(encoding="utf-8"))
        if "operationId" in content or "schema" in content:
            entries.append((path.name, content))
        else:
            entries.extend((f"{path.name}:{key}", value) for key, value in content.items())
    return entries


@pytest.mark.parametrize(("name", "fixture"), _fixture_entries(), ids=lambda v: str(v)[:40])
def test_fixture_matches_spec(name: str, fixture: dict[str, Any]) -> None:
    if "operationId" in fixture:
        op = OPERATIONS[fixture["operationId"]]
        validate_response_body(op.method, op.path, 200, fixture["body"])
    else:
        validate_schema(fixture["schema"], fixture["body"])


def test_all_six_scenarios_defined() -> None:
    assert set(load_scenarios()) == set(SCENARIO_KEYS)


@pytest.mark.parametrize("key", SCENARIO_KEYS)
def test_timeline_states_and_results_are_spec_values(key: str) -> None:
    for timeline in load_scenarios()[key].timelines.values():
        for step in timeline.steps:
            validate_schema("ESessionState", step.body["state"])
            if "result" in step.body:
                validate_schema("SessionResultModel", step.body["result"])
        assert timeline.steps[-1].body["state"] == "Stopped"
        for line in timeline.logs:
            validate_schema("ETaskLogRecordStatus", line.status)


def test_fixtures_dir_is_packaged_with_the_code() -> None:
    assert Path(FIXTURES).is_relative_to(Path(__file__).resolve().parents[2] / "src")

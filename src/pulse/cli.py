"""`pulse` command line.

    uv run pulse smoke --profile ops       # log in to the lab server, read job states, log out
    uv run pulse scenarios --speed 10      # run all six mock scenarios end to end

The web UI (`pulse serve`) arrives with Phase 3.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable

from pulse.config import ProfileError, Settings
from pulse.inspector.bus import InspectorBus, InspectorEvent
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError


def _print_events(events: Iterable[InspectorEvent], indent: str = "  ") -> None:
    for event in events:
        status = event.status if event.status is not None else "—"
        code = f" {event.error_code}" if event.error_code else ""
        print(f"{indent}{event.method:<5} {event.url}  {status}{code} · {event.duration_ms} ms")


async def _smoke(settings: Settings, profile_name: str) -> int:
    profile = settings.profile(profile_name)
    bus = InspectorBus()
    async with VbrClient(
        settings.vbr_url,
        profile.credentials,
        api_version=settings.api_version,
        verify=settings.tls_verify,
        inspector=bus,
        mode=settings.mode,
    ) as client:
        info = await client.request("GetServerInfo")
        states = await client.request("GetAllJobsStates", params={"limit": 5})
        print(f"Connected to {info.get('name')} · build {info.get('buildVersion')}")
        print(f"Jobs: {states['pagination']['total']}")
    _print_events(bus.recent())
    return 0


async def _scenarios(keys: list[str], speed: float, verbose: bool) -> int:
    from pulse.mock.runner import run_scenario  # imports FastAPI; keep `smoke` fast

    failures = 0
    for key in keys:
        report = await run_scenario(key, speed=speed)
        mark = "✓" if report.passed else "✕"
        print(f"{mark} {report.key} — {report.title}")
        for step in report.steps:
            print(f"    {step}")
        if report.error:
            failures += 1
            print(f"    {report.error}")
        if verbose:
            _print_events(report.events, indent="      ")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    from pulse.mock.scenarios import SCENARIO_KEYS

    parser = argparse.ArgumentParser(prog="pulse", description="VBR Pulse")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke", help="Log in to the configured server and read job states")
    smoke.add_argument("--profile", default="ops")
    scenarios = sub.add_parser("scenarios", help="Run the mock scenarios end to end")
    scenarios.add_argument("keys", nargs="*", metavar="scenario", help=", ".join(SCENARIO_KEYS))
    scenarios.add_argument("--speed", type=float, default=10.0, help="time compression factor")
    scenarios.add_argument("-v", "--verbose", action="store_true", help="print every API call")
    args = parser.parse_args(argv)
    if args.command == "scenarios" and (unknown := set(args.keys) - set(SCENARIO_KEYS)):
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")

    try:
        if args.command == "scenarios":
            code = asyncio.run(
                _scenarios(args.keys or list(SCENARIO_KEYS), args.speed, args.verbose)
            )
        else:
            settings = Settings()
            if settings.lab_insecure_tls:
                print("Certificate checks are off. Lab use only.", file=sys.stderr)
            code = asyncio.run(_smoke(settings, args.profile))
    except (VbrError, ProfileError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)

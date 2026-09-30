"""`pulse` command line.

    uv run pulse smoke --profile ops   # log in, read server info + job states, log out

The web UI (`pulse serve`) arrives with Phase 3.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pulse.config import ProfileError, Settings
from pulse.inspector.bus import InspectorBus
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError


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
    for event in bus.recent():
        status = event.status if event.status is not None else "—"
        print(f"  {event.method:<5} {event.url}  {status} · {event.duration_ms} ms")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pulse", description="VBR Pulse")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke", help="Log in to the configured server and read job states")
    smoke.add_argument("--profile", default="ops")
    args = parser.parse_args(argv)

    settings = Settings()
    if settings.lab_insecure_tls:
        print("Certificate checks are off. Lab use only.", file=sys.stderr)
    try:
        code = asyncio.run(_smoke(settings, args.profile))
    except (VbrError, ProfileError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)

"""`pulse` command line.

pulse                                  # serve the web UI on http://127.0.0.1:8000 (default)
pulse serve --mock                     # …with mock data preselected
pulse init                             # create the settings file (per-user folder)
pulse secret set svc-pulse-ops         # store an account's password in the OS keyring
pulse config                           # which settings file is used, and which secrets exist
pulse preflight [--mock]               # the checkable half of the pre-flight checklist
pulse smoke --profile ops              # log in to the lab server, read job states, log out
pulse scenarios --speed 10             # run all six mock scenarios end to end

Every command takes --config PATH to use a specific settings file.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Iterable
from pathlib import Path

import keyring

from pulse import __version__
from pulse.config import (
    CONFIG_TEMPLATE,
    ProfileError,
    Settings,
    delete_secret,
    has_secret,
    is_frozen,
    load_settings,
    store_secret,
    user_config_file,
)
from pulse.inspector.bus import InspectorBus, InspectorEvent
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


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


# ---------------------------------------------------------------- serve


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _any_free_port(host: str) -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _open_when_ready(server: object, url: str) -> None:
    for _ in range(200):
        if getattr(server, "started", False):
            webbrowser.open(url)
            return
        time.sleep(0.05)


def _serve(
    settings: Settings,
    host: str,
    port: int,
    allow_remote: bool,
    *,
    open_browser: bool = False,
    port_explicit: bool = True,
) -> int:
    import uvicorn

    from pulse.web.app import create_app

    if host not in LOOPBACK:
        if not allow_remote:
            print(
                f"Refusing to bind to {host}: VBR Pulse listens on 127.0.0.1 unless you pass "
                "--allow-remote.",
                file=sys.stderr,
            )
            return 2
        print(
            f"WARNING: VBR Pulse is reachable from other machines on {host}:{port}. "
            "Anyone who can reach it can use the signed-in account.",
            file=sys.stderr,
        )
    if not _port_free(host, port):
        if port_explicit:
            print(f"Port {port} is already in use. Pick another with --port.", file=sys.stderr)
            return 2
        busy, port = port, _any_free_port(host)
        print(f"Port {busy} is busy, so VBR Pulse is using {port} instead.")
    if settings.lab_insecure_tls:
        print("Certificate checks are off. Lab use only.", file=sys.stderr)

    url = f"http://{'127.0.0.1' if host == 'localhost' else host}:{port}"
    config_file = settings._env_path
    print(f"VBR Pulse {__version__} on {url}  (press Ctrl+C to stop)")
    if config_file is None:
        print("No settings file, so only mock data is available. Run `pulse init` to add a server.")
    else:
        print(f"Settings: {config_file}")

    # Explicit asyncio + h11: no optional speedups to import, which keeps release builds simple.
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=host,
            port=port,
            log_level="warning",
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
        )
    )
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    server.run()
    return 0


# ---------------------------------------------------------------- settings and secrets


def _init(explicit: Path | None, force: bool) -> int:
    target = explicit or user_config_file()
    if target.exists() and not force:
        print(f"{target} already exists. Edit it, or pass --force to start over.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")
    print("Next:")
    print("  1. Set PULSE_VBR_URL and the PULSE_PROFILE_*_USER accounts in that file.")
    print("  2. Store each account's password:  pulse secret set <username>")
    print("  3. Check everything:               pulse preflight")
    return 0


def _show_config(settings: Settings) -> int:
    path = settings._env_path
    print(f"VBR Pulse {__version__}")
    print(f"Settings file:  {path or 'none (mock data only)'}")
    print(f"Per-user file:  {user_config_file()}")
    backend = type(keyring.get_keyring())
    # e.g. "Windows.WinVaultKeyring" or "macOS.Keyring"
    print(
        f"Keyring:        {backend.__module__.removeprefix('keyring.backends.')}.{backend.__name__}"
    )
    if path is None:
        return 0
    print(f"Server:         {settings.vbr_url}")
    try:
        profiles = settings.profiles()
    except ProfileError as exc:
        print(f"Profiles:       {exc}")
        return 1
    for profile in profiles:
        secret = "password stored" if has_secret(profile) else "NO PASSWORD — pulse secret set"
        print(f"  {profile.name:<5} {profile.username:<20} {secret}")
    return 0


def _secret(action: str, username: str) -> int:
    if action == "delete":
        found = delete_secret(username)
        print(f"Removed the password for {username}." if found else f"No password for {username}.")
        return 0 if found else 1
    first = getpass.getpass(f"Password for {username}: ")
    if not first:
        print("Nothing stored.")
        return 1
    if getpass.getpass("Again, to confirm: ") != first:
        print("The passwords don't match. Nothing stored.")
        return 1
    store_secret(username, first)
    print(f"Stored the password for {username} in the OS keyring.")
    return 0


# ---------------------------------------------------------------- entry point


def _parser(scenario_keys: Iterable[str]) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="settings file to use (default: see `pulse config`)",
    )

    parser = argparse.ArgumentParser(prog="pulse", description="VBR Pulse", parents=[common])
    parser.add_argument("--version", action="version", version=f"VBR Pulse {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", parents=[common], help="Serve the web UI (default)")
    serve.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument(
        "--allow-remote", action="store_true", help="allow binding to a non-loopback address"
    )
    serve.add_argument("--mock", action="store_true", help="preselect mock data on sign-in")
    serve.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        default=None,
        help="open the browser (default in release builds)",
    )
    serve.add_argument("--no-open", dest="open_browser", action="store_false")

    init = sub.add_parser("init", parents=[common], help="Create the settings file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    sub.add_parser("config", parents=[common], help="Show the settings file and stored secrets")
    secret = sub.add_parser("secret", help="Store or remove an account's password")
    secret.add_argument("action", choices=["set", "delete"])
    secret.add_argument("username")

    preflight = sub.add_parser(
        "preflight", parents=[common], help="Check the lab (or mock mode) before a demo"
    )
    preflight.add_argument("--mock", action="store_true", help="check mock mode instead")
    smoke = sub.add_parser(
        "smoke", parents=[common], help="Log in to the configured server and read job states"
    )
    smoke.add_argument("--profile", default="ops")
    scenarios = sub.add_parser("scenarios", help="Run the mock scenarios end to end")
    scenarios.add_argument("keys", nargs="*", metavar="scenario", help=", ".join(scenario_keys))
    scenarios.add_argument("--speed", type=float, default=10.0, help="time compression factor")
    scenarios.add_argument("-v", "--verbose", action="store_true", help="print every API call")
    return parser


def _run(args: argparse.Namespace) -> int:
    from pulse.mock.scenarios import SCENARIO_KEYS

    explicit: Path | None = getattr(args, "config", None)
    if args.command == "init":
        return _init(explicit, args.force)
    if args.command == "secret":
        return _secret(args.action, args.username)
    if args.command == "scenarios":
        return asyncio.run(_scenarios(args.keys or list(SCENARIO_KEYS), args.speed, args.verbose))

    settings = load_settings(explicit)
    if args.command == "config":
        return _show_config(settings)
    if args.command == "preflight":
        from pulse.preflight import run as run_preflight

        return asyncio.run(run_preflight(settings, args.mock))
    if args.command == "smoke":
        if settings.lab_insecure_tls:
            print("Certificate checks are off. Lab use only.", file=sys.stderr)
        return asyncio.run(_smoke(settings, args.profile))

    # serve (the default: double-clicking a release build lands here)
    if getattr(args, "mock", False) or settings._env_path is None:
        settings.mock = True
    open_browser = getattr(args, "open_browser", None)
    return _serve(
        settings,
        getattr(args, "host", None) or settings.host,
        getattr(args, "port", None) or settings.port,
        getattr(args, "allow_remote", False),
        open_browser=is_frozen() if open_browser is None else open_browser,
        port_explicit=getattr(args, "port", None) is not None,
    )


def _utf8_output() -> None:
    """Print ✓, – and … even when Windows redirects output through a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> None:
    from pulse.mock.scenarios import SCENARIO_KEYS

    _utf8_output()
    parser = _parser(SCENARIO_KEYS)
    args = parser.parse_args(argv)
    if args.command == "scenarios" and (unknown := set(args.keys) - set(SCENARIO_KEYS)):
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")

    try:
        code = _run(args)
    except (VbrError, ProfileError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 0
    # A double-clicked release build runs in its own console window: keep errors readable.
    if code != 0 and is_frozen() and len(sys.argv) <= 1:
        input("Press Enter to close this window…")
    sys.exit(code)

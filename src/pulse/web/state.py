"""Server-side state: one Connection per signed-in browser session (PLAN §4.1, §9).

The browser only ever holds an opaque, HttpOnly session cookie. Credentials, tokens
and the VbrClient live here, in memory.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pulse.config import MOCK_PROFILES, Profile, ProfileError, Settings
from pulse.inspector.bus import InspectorBus
from pulse.mock.app import MOCK_BASE_URL, mock_transport
from pulse.mock.state import MockVbr
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError
from pulse.vbr.sessions import SessionTracker

Mode = Literal["live", "mock"]
COOKIE = "pulse_session"


@dataclass
class IncidentState:
    """Where the presenter is in the three-step incident flow (PLAN §7.5.5)."""

    event: dict[str, Any] | None = None
    backup_object: dict[str, Any] | None = None
    quick_session: str | None = None
    quick_result: str | None = None
    scan_session: str | None = None
    scan_result: str | None = None
    error: str | None = None

    @property
    def step(self) -> int:
        """The step that is currently unlocked (1–3), or 4 when everything is done."""
        if self.event is None:
            return 1
        if self.quick_result not in ("Success", "Warning"):
            return 2
        if self.scan_result not in ("Success", "Warning"):
            return 3
        return 4


@dataclass
class Connection:
    id: str
    profile: Profile
    mode: Mode
    server_url: str
    client: VbrClient
    tracker: SessionTracker
    server_info: dict[str, Any] | None = None
    incident: IncidentState = field(default_factory=IncidentState)
    job_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    dock_session: str | None = None
    connected_at: float = field(default_factory=time.time)

    @property
    def username(self) -> str:
        return self.profile.username

    @property
    def host(self) -> str:
        return self.server_url.split("://", 1)[-1].split("/", 1)[0]

    @property
    def server_name(self) -> str:
        return (self.server_info or {}).get("name") or self.host

    async def close(self) -> None:
        await self.tracker.aclose()
        await self.client.aclose()


class SignInError(Exception):
    """Safe-to-display reason a sign-in failed."""


class AppState:
    def __init__(self, settings: Settings, *, mock: MockVbr | None = None) -> None:
        self.settings = settings
        self.bus = InspectorBus()
        self._mock = mock
        self.connections: dict[str, Connection] = {}
        self.poll_seconds = float(settings.poll_seconds)

    @property
    def mock(self) -> MockVbr:
        if self._mock is None:
            self._mock = MockVbr(self.settings.mock_scenario)
        return self._mock

    def profiles(self, mode: Mode) -> list[Profile]:
        if mode == "mock":
            return list(MOCK_PROFILES)
        return self.settings.profiles()

    def profile(self, mode: Mode, name: str) -> Profile:
        for profile in self.profiles(mode):
            if profile.name == name:
                return profile
        raise SignInError(f"There's no '{name}' profile.")

    def get(self, conn_id: str | None) -> Connection | None:
        return self.connections.get(conn_id or "")

    async def connect(
        self, mode: Mode, profile_name: str, server_url: str | None = None
    ) -> Connection:
        """CreateToken then GetServerInfo (PLAN §7.5.1). Raises SignInError on failure."""
        if mode == "mock" and profile_name not in {p.name for p in MOCK_PROFILES}:
            profile_name = "ops"  # live profile names differ; the mock only knows the demo three
        try:
            profile = self.profile(mode, profile_name)
        except ProfileError as exc:
            raise SignInError(str(exc)) from exc
        url = MOCK_BASE_URL if mode == "mock" else (server_url or self.settings.vbr_url).strip()
        if mode == "live" and not url.startswith("https://"):
            raise SignInError("Use an https:// address for the backup server.")

        client = VbrClient(
            url,
            profile.credentials,
            api_version=self.settings.api_version,
            verify=self.settings.tls_verify,
            inspector=self.bus,
            mode=mode,
            transport=mock_transport(self.mock) if mode == "mock" else None,
        )
        try:
            await client.login()
        except ProfileError as exc:
            await client.aclose()
            raise SignInError(str(exc)) from exc
        except VbrError as exc:
            await client.aclose()
            if exc.status == 401:
                raise SignInError(
                    f"The server rejected the credentials for {profile.username}."
                ) from exc
            raise SignInError(exc.message) from exc

        conn = Connection(
            id=secrets.token_urlsafe(32),
            profile=profile,
            mode=mode,
            server_url=url,
            client=client,
            tracker=SessionTracker(client, poll_seconds=self.poll_seconds),
        )
        try:
            conn.server_info = await client.request("GetServerInfo")
        except VbrError:
            # Some roles can't read server info; degrade to the host name (PLAN §5.4).
            conn.server_info = None
        self.connections[conn.id] = conn
        return conn

    async def disconnect(self, conn_id: str | None) -> None:
        conn = self.connections.pop(conn_id or "", None)
        if conn is not None:
            await conn.close()

    def role_of(self, conn: Connection) -> str | None:
        if conn.mode == "mock":
            return self.mock.role_of(conn.username)
        return conn.profile.role

    def set_poll_seconds(self, seconds: float) -> None:
        self.poll_seconds = seconds
        for conn in self.connections.values():
            conn.tracker.poll_seconds = seconds

    async def aclose(self) -> None:
        for conn_id in list(self.connections):
            await self.disconnect(conn_id)

"""`pulse preflight`: the checkable half of the pre-flight checklist (PLAN Appendix D).

Live mode checks the lab server itself: reachability on 443, every profile can sign in,
the tagged demo job exists and is idle, and (for the incident story) a malware event
exists. Mock mode runs all six scenarios. The human half is printed as a reminder.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulse.config import ProfileError, Settings
from pulse.vbr.client import VbrClient
from pulse.vbr.errors import VbrError

DEMO_TAG = "[pulse-demo]"
MANUAL = (
    "Display at 1920×1080 or 1366×768, browser zoom 100 %, notifications off",
    "Presenter mode on, inspector open, token ring visible",
    "Fallback video on the desktop",
)


@dataclass
class Check:
    ok: bool
    text: str


async def live_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    try:
        profiles = settings.profiles()
    except ProfileError as exc:
        return [Check(False, str(exc))]

    for profile in profiles:
        client = VbrClient(settings.vbr_url, profile.credentials, verify=settings.tls_verify)
        try:
            await client.request("GetServerTime")
            checks.append(Check(True, f"{profile.username} signs in; server answers on 443"))
            if profile.name == "ops":
                checks.append(await _demo_job(client))
            if profile.name == "ir":
                events = await client.request("ViewSuspiciousActivityEvents", params={"limit": 1})
                total = events["pagination"]["total"]
                checks.append(
                    Check(
                        total > 0,
                        f"{total} malware event(s) for the incident story"
                        + ("" if total else " — seed one from Settings"),
                    )
                )
        except (VbrError, ProfileError) as exc:
            checks.append(Check(False, f"{profile.username}: {exc}"))
        finally:
            await client.aclose()
    return checks


async def _demo_job(client: VbrClient) -> Check:
    async for job in client.paginate("GetAllJobsStates"):
        if DEMO_TAG in (job.get("description") or ""):
            idle = job["status"] in ("Inactive", "Enabled", "Stopped")
            return Check(idle, f"Demo job '{job['name']}' is {'idle' if idle else job['status']}")
    return Check(False, f"No job's description contains {DEMO_TAG}")


async def mock_checks() -> list[Check]:
    from pulse.mock.runner import run_all

    return [
        Check(
            report.passed,
            f"Mock scenario '{report.key}'" + ("" if report.passed else f": {report.error}"),
        )
        for report in await run_all(speed=40.0)
    ]


async def run(settings: Settings, mock: bool) -> int:
    checks = await (mock_checks() if mock else live_checks(settings))
    for check in checks:
        print(f"{'✓' if check.ok else '✕'} {check.text}")
    print("\nStill to check by hand:")
    for item in MANUAL:
        print(f"  ☐ {item}")
    return 0 if all(c.ok for c in checks) else 1

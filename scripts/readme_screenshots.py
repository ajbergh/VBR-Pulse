"""Regenerate the README screenshots in docs/images/ from mock mode (no lab data).

    uv run python scripts/readme_screenshots.py

Uses the installed Edge on Windows, Playwright's Chromium elsewhere
(`uv run playwright install chromium`). Takes about three minutes: sessions run in real time.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.e2e.harness import browser_channel, running_app  # noqa: E402

OUT = ROOT / "docs" / "images"
VIEWPORT = {"width": 1600, "height": 900}


def wait_until(page: Page, expression: str, timeout: float = 90.0) -> None:
    # page.wait_for_function would be blocked by the app's CSP; poll from Python instead.
    for _ in range(int(timeout * 10)):
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise TimeoutError(expression)


def settle(page: Page) -> None:
    """Park the mouse and clear toasts so nothing transient lands in a screenshot."""
    page.mouse.move(VIEWPORT["width"] - 5, VIEWPORT["height"] - 5)
    page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")
    page.wait_for_timeout(700)


def shoot(page: Page, name: str) -> None:
    settle(page)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"wrote docs/images/{name}.png")


def sign_in(page: Page, base: str, profile: str = "ops") -> None:
    page.goto(f"{base}/signin")
    page.select_option("#profile", profile)
    page.locator("button[value=mock]").first.click()
    page.wait_for_url("**/incident" if profile == "ir" else "**/jobs")


def pick_scenario(page: Page, title: str) -> None:
    page.keyboard.press("s")
    page.get_by_label(title).check()
    page.get_by_role("button", name="Use scenario").click()
    page.wait_for_timeout(500)


def start(page: Page, job: str) -> None:
    page.locator("#job-filter").fill(job)
    button = page.get_by_role("button", name=f"Start {job}", exact=True)
    button.wait_for()
    button.click()
    page.locator("#session-dock .track").wait_for()


def progress_over(page: Page, percent: int) -> None:
    wait_until(
        page,
        f"Number(document.querySelector('#session-dock .track-bar')"
        f".getAttribute('aria-valuenow')) >= {percent}",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with running_app(poll_seconds=5) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel())

        def new_page() -> Page:
            return browser.new_context(viewport=VIEWPORT).new_page()  # type: ignore[arg-type]

        # Sign-in
        page = new_page()
        page.goto(f"{base}/signin")
        shoot(page, "signin")

        # Jobs + live session track (the focal element)
        sign_in(page, base)
        start(page, "SQL Daily")
        progress_over(page, 55)
        shoot(page, "jobs-live-session")

        # Inspector: the StartJob call, expanded, wide
        page.locator("#inspector-rows li", has_text="/start").first.locator("summary").click()
        page.get_by_role("button", name="Copy as cURL").first.wait_for()
        page.locator('[data-action="inspector-wide"]').click()
        shoot(page, "inspector")
        page.locator('[data-action="inspector-wide"]').click()

        # Failure: logs appear automatically
        page = new_page()
        sign_in(page, base)
        pick_scenario(page, "Failure")
        start(page, "Finance close")
        page.locator("#session-dock .track.is-ended").wait_for(timeout=90_000)
        page.locator("#session-dock").scroll_into_view_if_needed()
        shoot(page, "session-failed")

        # RBAC: the viewer is refused, and the callout names the role
        page = new_page()
        sign_in(page, base, "view")
        pick_scenario(page, "Happy path")
        page.locator("#job-filter").fill("Payroll")
        page.get_by_role("button", name="Start Payroll", exact=True).click()
        page.locator(".callout-forbidden").wait_for()
        shoot(page, "rbac-forbidden")

        # Repositories
        page = new_page()
        sign_in(page, base)
        page.goto(f"{base}/repositories")
        page.select_option("#repo-sort", "free")
        page.wait_for_timeout(800)
        shoot(page, "repositories")

        # Incident response (13.1): event → quick backup → scan
        page = new_page()
        sign_in(page, base)
        pick_scenario(page, "Incident response")
        sign_in(page, base, "ir")
        page.locator(".event-choice input").first.check()
        page.get_by_role("button", name="Respond to this event").click()
        page.get_by_role("button", name="Start quick backup").click()
        page.get_by_role("button", name="Scan latest restore point").click(timeout=90_000)
        page.locator(".callout-ok").wait_for(timeout=90_000)
        shoot(page, "incident")

        # Presenter mode
        page = new_page()
        sign_in(page, base)
        pick_scenario(page, "Happy path")
        page.keyboard.press("p")
        start(page, "Web tier")
        progress_over(page, 40)
        shoot(page, "presenter-mode")

        browser.close()


if __name__ == "__main__":
    main()

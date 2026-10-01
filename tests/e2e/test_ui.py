"""Phase 4 acceptance: screenshots, layout invariants, axe-core scan, keyboard-only walkthrough.

Run with `uv run pytest -m e2e`. Uses an installed Edge on Windows (no browser download);
elsewhere Playwright's Chromium (`uv run playwright install chromium`). Screenshots land in
test-results/screenshots/ for review against PLAN §7.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect, sync_playwright

from tests.e2e.harness import browser_channel, running_app

pytestmark = pytest.mark.e2e

SHOTS = Path(__file__).resolve().parents[2] / "test-results" / "screenshots"
SIZES = {"1920x1080": {"width": 1920, "height": 1080}, "1366x768": {"width": 1366, "height": 768}}


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    with running_app(poll_seconds=1) as url:
        yield url


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(channel=browser_channel())
        yield instance
        instance.close()


def sign_in(browser: Browser, base_url: str, size: str = "1920x1080", profile: str = "ops") -> Page:
    page = browser.new_context(viewport=SIZES[size]).new_page()  # type: ignore[arg-type]
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    page.goto(f"{base_url}/signin")
    page.select_option("#profile", profile)
    page.locator("button[value=mock]").first.click()
    page.wait_for_url("**/incident" if profile == "ir" else "**/jobs")
    return page


def wait_until(page: Page, expression: str, timeout: float = 30.0) -> None:
    """Poll from Python: the app's CSP (rightly) blocks page.wait_for_function's eval."""
    for _ in range(int(timeout * 10)):
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"Timed out waiting for: {expression}")


def start_job(page: Page, name: str) -> None:
    page.locator("#job-filter").fill(name)
    start = page.get_by_role("button", name=f"Start {name}", exact=True)
    start.wait_for()
    start.click()
    expect(page.locator("#session-dock .track")).to_be_visible()


# ---------------------------------------------------------------- screenshots + layout


@pytest.mark.parametrize("presenter", [False, True], ids=["normal", "presenter"])
@pytest.mark.parametrize("size", list(SIZES))
def test_jobs_screen_layout(browser: Browser, base_url: str, size: str, presenter: bool) -> None:
    job = {
        ("1920x1080", False): "Web tier",
        ("1920x1080", True): "App tier",
        ("1366x768", False): "Payroll",
        ("1366x768", True): "Intranet",
    }[(size, presenter)]
    page = sign_in(browser, base_url, size)
    if presenter:
        page.keyboard.press("p")
    start_job(page, job)
    wait_until(
        page, "Number(document.querySelector('.track-bar').getAttribute('aria-valuenow')) > 20"
    )
    mode = "presenter" if presenter else "normal"
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"jobs-{size}-{mode}.png"))

    metrics = page.evaluate(
        """() => ({
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        inspector: document.querySelector('#inspector').getBoundingClientRect().width,
        rail: document.querySelector('.rail').getBoundingClientRect().width,
        pct: getComputedStyle(document.querySelector('.track-pct')).fontSize,
        body: getComputedStyle(document.body).fontSize,
        track: document.querySelector('.track-bar').getBoundingClientRect().height,
        dockBottom: document.querySelector('#session-dock').getBoundingClientRect().bottom,
        })"""
    )
    assert metrics["overflow"] <= 0, "the page scrolls sideways"
    assert metrics["inspector"] == pytest.approx(420, abs=1)
    assert metrics["dockBottom"] <= SIZES[size]["height"] + 1, "the session track is off screen"
    if presenter:
        assert (metrics["pct"], metrics["body"], metrics["track"]) == ("88px", "20px", 24)
        assert metrics["rail"] == pytest.approx(72, abs=1)
    else:
        assert (metrics["pct"], metrics["body"], metrics["track"]) == ("64px", "16px", 16)
        assert metrics["rail"] == pytest.approx(200, abs=1)
    assert page.errors == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", ["repositories", "sessions", "settings"])
def test_other_screens_render(browser: Browser, base_url: str, path: str) -> None:
    page = sign_in(browser, base_url)
    page.goto(f"{base_url}/{path}")
    page.screenshot(path=str(SHOTS / f"{path}-1920x1080.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.errors == []  # type: ignore[attr-defined]


def test_repository_cards_per_row(browser: Browser, base_url: str) -> None:
    for size, expected in (("1920x1080", 3), ("1366x768", 2)):
        page = sign_in(browser, base_url, size)
        page.goto(f"{base_url}/repositories")
        tops = page.eval_on_selector_all(".repo-card", "cards => cards.map(c => c.offsetTop)")
        assert tops.count(tops[0]) == expected, size


# ---------------------------------------------------------------- accessibility (WCAG 2.2 AA)


def serious(page: Page) -> list[str]:
    results = Axe().run(page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]})
    return [
        f"{v['id']} ({v['impact']}): {[n['target'] for n in v['nodes']][:3]}"
        for v in results.response["violations"]
        if v["impact"] in ("serious", "critical")
    ]


def test_axe_sign_in(browser: Browser, base_url: str) -> None:
    page = browser.new_page()
    page.goto(f"{base_url}/signin")
    assert serious(page) == []


@pytest.mark.parametrize("path", ["jobs", "repositories", "sessions", "incident", "settings"])
@pytest.mark.parametrize("presenter", [False, True], ids=["normal", "presenter"])
def test_axe_screens(browser: Browser, base_url: str, path: str, presenter: bool) -> None:
    page = sign_in(browser, base_url)
    page.goto(f"{base_url}/{path}")
    if presenter:
        page.keyboard.press("p")
    assert serious(page) == []


def test_axe_running_track_and_open_inspector_row(browser: Browser, base_url: str) -> None:
    page = sign_in(browser, base_url)
    start_job(page, "Mail relays")
    page.locator("#inspector-rows summary").first.click()
    expect(page.get_by_role("button", name="Copy as cURL").first).to_be_visible()
    page.keyboard.press("?")
    expect(page.locator("#shortcuts")).to_be_visible()
    assert serious(page) == []


# ---------------------------------------------------------------- keyboard only


def focused(page: Page) -> str:
    return str(
        page.evaluate(
            "(document.activeElement.getAttribute('aria-label') || document.activeElement.innerText"
            " || document.activeElement.id || '').trim()"
        )
    )


def tab_until(page: Page, predicate: str, limit: int = 40) -> None:
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate(predicate):
            return
    raise AssertionError(f"Tab never reached: {predicate}")


def test_keyboard_walkthrough(browser: Browser, base_url: str) -> None:
    page = browser.new_page(viewport=SIZES["1920x1080"])
    page.goto(f"{base_url}/signin")
    tab_until(page, "document.activeElement.id === 'profile'")
    page.keyboard.press("Tab")
    assert "Connect" in focused(page)
    page.keyboard.press("Enter")
    page.wait_for_url("**/jobs")

    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "job-filter"
    page.keyboard.type("DNS")
    page.get_by_role("button", name="Start DNS and DHCP").wait_for()
    tab_until(page, "document.activeElement.innerText.startsWith('Start')")
    page.keyboard.press("Enter")
    wait_until(page, "document.activeElement.classList.contains('track')")

    page.keyboard.press("i")
    assert page.evaluate("document.documentElement.classList.contains('inspector-collapsed')")
    page.keyboard.press("i")
    tab_until(page, "!!document.activeElement.closest('#inspector-rows summary')")
    page.keyboard.press("Enter")
    expect(page.locator("#inspector-rows details[open] .insp-actions").first).to_be_visible()

    page.keyboard.press("?")
    expect(page.locator("#shortcuts")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#shortcuts")).to_be_hidden()

    page.keyboard.press("2")
    page.wait_for_url("**/repositories")


def test_expanded_polling_row_survives_new_polls(browser: Browser, base_url: str) -> None:
    """A grouped row is replaced on every poll; it must stay open and keep focus."""
    page = sign_in(browser, base_url)
    start_job(page, "Identity services")
    session_id = page.locator("#session-dock .track").get_attribute("id").removeprefix("track-")  # type: ignore[union-attr]
    row_selector = f"#insp-grp-session-{session_id}"
    row = page.locator(row_selector)
    row.wait_for()
    row.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(row.locator(".insp-actions")).to_be_visible()
    count_before = int(row.locator(".insp-count").inner_text().lstrip("×"))

    page.wait_for_timeout(3500)  # the harness polls every second

    assert row.locator("details").get_attribute("open") is not None, "the row snapped shut"
    assert int(row.locator(".insp-count").inner_text().lstrip("×")) > count_before
    expect(row.locator(".insp-actions")).to_be_visible()
    assert page.evaluate(f"!!document.activeElement.closest('{row_selector}')"), (
        "keyboard focus left the row"
    )


@pytest.mark.parametrize("presenter", [False, True], ids=["normal", "presenter"])
@pytest.mark.parametrize("size", list(SIZES))
def test_full_job_table_fits_beside_the_inspector(
    browser: Browser, base_url: str, size: str, presenter: bool
) -> None:
    """Long dates ("Oct 1 04:09") must wrap rather than push Start off the edge."""
    page = sign_in(browser, base_url, size)
    if presenter:
        page.keyboard.press("p")
    overflow = page.evaluate(
        "document.querySelector('.jobs-table').getBoundingClientRect().width"
        " - document.querySelector('#jobs-table').clientWidth"
    )
    assert overflow <= 1, f"the job table is {overflow:.0f} px wider than its area"

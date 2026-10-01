"""Record animated GIFs of VBR Pulse in action, from mock mode (no lab data), into docs/images/.

    uv run python scripts/readme_gifs.py            # all clips (~6 minutes: sessions run live)
    uv run python scripts/readme_gifs.py incident   # just one

Clicks and typing play back in real time; waits for sessions are time-lapsed. A cursor dot
and on-screen key labels are drawn into the page, because headless screenshots have no pointer.
"""

from __future__ import annotations

import io
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageColor
from playwright.sync_api import Locator, Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.e2e.harness import browser_channel, running_app  # noqa: E402

OUT = ROOT / "docs" / "images"
# PLAN §7.2 tokens plus the text-safe shades in pulse.css.
BRAND_COLOURS = [
    "#00D15F",
    "#009277",
    "#E1F4EC",
    "#3700FF",
    "#EEF4F6",
    "#232323",
    "#505861",
    "#ADACAF",
    "#F0F0F0",
    "#F9F9F9",
    "#FFFFFF",
    "#ED2B3D",
    "#FE8A25",
    "#FFD839",
    "#077C66",
    "#C42A38",
    "#A25D1E",
]
VIEWPORT = {"width": 1366, "height": 768}  # the plan's projector size

# Drawn into every page: a pointer that follows the mouse and shrinks on click.
CURSOR_JS = """
(() => {
  const install = () => {
    if (document.getElementById('__rec_cursor')) return;
    const c = document.createElement('div');
    c.id = '__rec_cursor';
    c.style.cssText = 'position:fixed;left:-40px;top:-40px;width:22px;height:22px;' +
      'margin:-11px 0 0 -11px;border-radius:50%;background:rgba(55,0,255,.28);' +
      'border:2px solid #3700FF;pointer-events:none;z-index:2147483647;' +
      'transition:transform .12s ease-out';
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => {
      c.style.left = e.clientX + 'px';
      c.style.top = e.clientY + 'px';
    }, true);
    addEventListener('mousedown', () => { c.style.transform = 'scale(.55)'; }, true);
    addEventListener('mouseup', () => { c.style.transform = 'scale(1)'; }, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', install);
  else install();
})();
"""

KEY_JS = """
label => {
  let k = document.getElementById('__rec_key');
  if (!k) {
    k = document.createElement('div');
    k.id = '__rec_key';
    k.style.cssText = 'position:fixed;left:24px;bottom:24px;padding:8px 16px;border-radius:8px;' +
      'background:#232323;color:#fff;font:600 20px "Source Sans 3",sans-serif;' +
      'z-index:2147483647;pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.25)';
    document.documentElement.appendChild(k);
  }
  k.textContent = label;
  k.style.display = label ? 'block' : 'none';
}
"""


class Recorder:
    """Collects (frame, duration) pairs from a page and writes them as one GIF."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.frames: list[tuple[Image.Image, int]] = []
        self.mouse = (VIEWPORT["width"] * 0.6, VIEWPORT["height"] * 0.55)

    # ------------------------------------------------------------ capture

    def snap(self, ms: int) -> None:
        png = self.page.screenshot(animations="allow")
        self.frames.append((Image.open(io.BytesIO(png)).convert("RGB"), ms))

    def hold(self, ms: int) -> None:
        self.snap(ms)

    def film(self, seconds: float, *, every: float = 1.0, speedup: float = 5.0) -> None:
        """Time-lapse: one frame per `every` real seconds, played back `speedup` times faster."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            started = time.monotonic()
            self.snap(int(every * 1000 / speedup))
            time.sleep(max(0.0, every - (time.monotonic() - started)))

    def film_until(
        self, expression: str, *, every: float = 1.0, speedup: float = 5.0, timeout: float = 120.0
    ) -> None:
        end = time.monotonic() + timeout
        while not self.page.evaluate(expression):
            if time.monotonic() > end:
                raise TimeoutError(expression)
            started = time.monotonic()
            self.snap(int(every * 1000 / speedup))
            time.sleep(max(0.0, every - (time.monotonic() - started)))

    # ------------------------------------------------------------ acting

    def restore_cursor(self) -> None:
        """After a navigation the overlay is new; put it back where the mouse is."""
        self.page.mouse.move(*self.mouse)

    def glide(self, target: Locator, steps: int = 10) -> None:
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box is not None, target
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        x0, y0 = self.mouse
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)
            self.page.mouse.move(x0 + (x - x0) * ease, y0 + (y - y0) * ease)
            if i % 2 == 0:
                self.snap(40)
        self.mouse = (x, y)

    def click(self, target: Locator) -> None:
        self.glide(target)
        self.page.mouse.down()
        self.snap(120)
        self.page.mouse.up()
        self.snap(80)

    def type(self, target: Locator, text: str) -> None:
        self.click(target)
        for char in text:
            self.page.keyboard.type(char)
            self.snap(110)

    def key(self, key: str, label: str) -> None:
        self.page.evaluate(KEY_JS, f"{label}")
        self.snap(500)
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(250)
        self.snap(700)
        self.page.evaluate(KEY_JS, "")

    # ------------------------------------------------------------ output

    def save(self, name: str) -> Path:
        # One palette for every frame: no colour shimmer, and Pillow can store just the
        # region that changed between frames. The brand colours are reserved entries, so a
        # red failure bar stays exactly red; the rest is fitted to frames from the whole clip.
        step = max(1, len(self.frames) // 40)
        sample = [f for f, _ in self.frames[::step]] + [self.frames[-1][0]]
        thumbs = [f.resize((f.width // 2, f.height // 2)) for f in sample]
        sheet = Image.new("RGB", (thumbs[0].width, thumbs[0].height * len(thumbs)))
        for i, thumb in enumerate(thumbs):
            sheet.paste(thumb, (0, i * thumb.height))
        reserved = [ImageColor.getrgb(c) for c in BRAND_COLOURS]
        fitted = sheet.quantize(colors=256 - len(reserved), method=Image.Quantize.MEDIANCUT)
        fitted_rgb = fitted.getpalette()[: 3 * (256 - len(reserved))]  # type: ignore[index]
        flat = [v for rgb in reserved for v in rgb] + fitted_rgb
        palette = Image.new("P", (1, 1))
        palette.putpalette(flat + [0] * (768 - len(flat)))
        frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in self.frames]
        durations = [max(20, ms) for _, ms in self.frames]
        path = OUT / f"{name}.gif"
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            optimize=False,
            disposal=1,
        )
        seconds = sum(durations) / 1000
        print(
            f"wrote docs/images/{name}.gif ({path.stat().st_size / 1_048_576:.1f} MB, "
            f"{len(frames)} frames, {seconds:.0f} s)"
        )
        return path


# ---------------------------------------------------------------- the clips


def sign_in(rec: Recorder, base: str, profile: str = "ops") -> None:
    page = rec.page
    page.goto(f"{base}/signin")
    page.select_option("#profile", profile)
    page.locator("button[value=mock]").first.click()
    page.wait_for_url("**/incident" if profile == "ir" else "**/jobs")
    page.wait_for_timeout(400)
    rec.restore_cursor()


def pick_scenario(rec: Recorder, title: str, *, show: bool = False) -> None:
    page = rec.page
    if show:
        rec.key("s", "S  ·  scenario picker")
        rec.click(page.get_by_label(title))
        rec.click(page.get_by_role("button", name="Use scenario"))
        rec.hold(600)
    else:
        page.keyboard.press("s")
        page.get_by_label(title).check()
        page.get_by_role("button", name="Use scenario").click()
    page.wait_for_timeout(300)
    page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")


def clear_toasts(rec: Recorder) -> None:
    rec.page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")


def track_ended() -> str:
    return "!!document.querySelector('#session-dock .track.is-ended')"


def clip_start_job(rec: Recorder, base: str) -> None:
    """The core demo: filter, start, watch the session, see every call in the inspector."""
    sign_in(rec, base)
    pick_scenario(rec, "Happy path")
    rec.hold(1200)
    rec.type(rec.page.locator("#job-filter"), "SQL")
    rec.page.wait_for_timeout(900)
    rec.hold(1000)
    rec.click(rec.page.get_by_role("button", name="Start SQL Daily", exact=True))
    rec.page.locator("#session-dock .track").wait_for()
    rec.film_until(track_ended(), every=1.0, speedup=5)
    rec.page.wait_for_timeout(800)
    rec.hold(3000)


def clip_inspector(rec: Recorder, base: str) -> None:
    """Open the StartJob call: redacted token, body, response, copy as cURL."""
    sign_in(rec, base)
    pick_scenario(rec, "Happy path")
    rec.page.locator("#job-filter").fill("Web tier")
    rec.page.get_by_role("button", name="Start Web tier", exact=True).click()
    rec.page.locator("#session-dock .track").wait_for()
    rec.page.wait_for_timeout(1500)
    clear_toasts(rec)
    rec.restore_cursor()
    rec.hold(800)
    rec.click(rec.page.locator('[data-action="inspector-wide"]'))
    rec.hold(600)
    row = rec.page.locator("#inspector-rows li", has_text="/start").first
    rec.click(row.locator("summary"))
    rec.page.get_by_role("button", name="Copy as cURL").first.wait_for()
    rec.hold(1800)
    rec.glide(row.locator(".insp-headers"))
    rec.hold(1500)
    rec.click(row.get_by_role("button", name="Copy as cURL"))
    rec.hold(1600)
    rec.click(rec.page.locator('[data-action="inspector-wide"]'))
    rec.hold(1200)


def clip_failure(rec: Recorder, base: str) -> None:
    """Pick the failure scenario, run a job, and watch Pulse fetch the logs."""
    sign_in(rec, base)
    rec.hold(800)
    pick_scenario(rec, "Failure", show=True)
    rec.type(rec.page.locator("#job-filter"), "Finance")
    rec.page.wait_for_timeout(900)
    rec.hold(600)
    rec.click(rec.page.get_by_role("button", name="Start Finance close", exact=True))
    rec.page.locator("#session-dock .track").wait_for()
    rec.film_until(track_ended(), every=1.0, speedup=5)
    rec.page.wait_for_timeout(800)
    rec.page.locator("#session-dock").scroll_into_view_if_needed()
    rec.hold(3500)


def clip_rbac(rec: Recorder, base: str) -> None:
    """A Backup Viewer is refused (403); switch to the operator and it works."""
    sign_in(rec, base, "view")
    pick_scenario(rec, "Happy path")
    rec.page.locator("#job-filter").fill("Payroll")
    rec.page.get_by_role("button", name="Start Payroll", exact=True).wait_for()
    rec.hold(1200)
    rec.click(rec.page.get_by_role("button", name="Start Payroll", exact=True))
    rec.page.locator(".callout-forbidden").wait_for()
    rec.hold(2800)
    rec.click(rec.page.get_by_role("button", name="Switch to operator"))
    rec.page.wait_for_url("**/jobs")
    rec.page.wait_for_timeout(600)
    rec.restore_cursor()
    rec.hold(1000)
    rec.type(rec.page.locator("#job-filter"), "Payroll")
    rec.page.wait_for_timeout(900)
    rec.click(rec.page.get_by_role("button", name="Start Payroll", exact=True))
    rec.page.locator("#session-dock .track").wait_for()
    rec.film(8, every=1.0, speedup=4)
    rec.hold(1500)


def clip_incident(rec: Recorder, base: str) -> None:
    """13.1 incident response: malware event → Quick Backup → backup scan."""
    sign_in(rec, base)
    pick_scenario(rec, "Incident response")
    sign_in(rec, base, "ir")
    rec.hold(1500)
    page = rec.page
    rec.click(page.locator(".event-choice").first)
    rec.click(page.get_by_role("button", name="Respond to this event"))
    page.get_by_role("button", name="Start quick backup").wait_for()
    rec.hold(1500)
    rec.click(page.get_by_role("button", name="Start quick backup"))
    rec.film_until(
        "!!document.querySelector('.stepper .step:nth-child(3) .btn-primary')", every=1.0, speedup=5
    )
    rec.hold(1200)
    rec.click(page.get_by_role("button", name="Scan latest restore point"))
    rec.film_until("!!document.querySelector('.callout-ok')", every=1.0, speedup=5)
    page.locator(".callout-ok").scroll_into_view_if_needed()
    rec.hold(3500)


def clip_presenter(rec: Recorder, base: str) -> None:
    """Presenter mode for the back row: P toggles it, I toggles the inspector."""
    sign_in(rec, base)
    pick_scenario(rec, "Happy path")
    rec.page.locator("#job-filter").fill("CRM")
    rec.page.get_by_role("button", name="Start CRM database", exact=True).click()
    rec.page.locator("#session-dock .track").wait_for()
    rec.page.wait_for_timeout(3000)
    clear_toasts(rec)
    rec.restore_cursor()
    rec.hold(1500)
    rec.key("p", "P  ·  presenter mode")
    rec.film(4, every=1.0, speedup=2)
    rec.key("i", "I  ·  hide the inspector")
    rec.film(4, every=1.0, speedup=2)
    rec.key("i", "I  ·  show the inspector")
    rec.key("p", "P  ·  presenter mode off")
    rec.hold(1200)


CLIPS: dict[str, Callable[[Recorder, str], None]] = {
    "demo-start-job": clip_start_job,
    "demo-inspector": clip_inspector,
    "demo-failure": clip_failure,
    "demo-rbac": clip_rbac,
    "demo-incident": clip_incident,
    "demo-presenter": clip_presenter,
}


def main() -> None:
    wanted = [f"demo-{a.removeprefix('demo-')}" for a in sys.argv[1:]] or list(CLIPS)
    unknown = set(wanted) - set(CLIPS)
    if unknown:
        raise SystemExit(
            f"Unknown clip(s): {', '.join(sorted(unknown))}. Known: {', '.join(CLIPS)}"
        )
    OUT.mkdir(parents=True, exist_ok=True)
    with running_app(poll_seconds=5) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel())
        for name in wanted:
            context = browser.new_context(viewport=VIEWPORT)  # type: ignore[arg-type]
            context.add_init_script(CURSOR_JS)
            recorder = Recorder(context.new_page())
            CLIPS[name](recorder, base)
            recorder.save(name)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()

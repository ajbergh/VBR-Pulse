"""Build, smoke-test and zip a standalone VBR Pulse release for this machine's platform.

    uv sync --group release
    uv run python scripts/build_release.py            # build + smoke test + zip
    uv run python scripts/build_release.py --no-smoke # build + zip only

Output: dist/vbr-pulse-<version>-<platform>.zip and a .sha256 next to it.
PyInstaller can't cross-compile: run this on Windows for Windows, on a Mac for macOS.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_DIR = DIST / "vbr-pulse"
sys.path.insert(0, str(ROOT / "src"))

from pulse import __version__  # noqa: E402

README_FIRST = """\
VBR Pulse {version}
==================

A demo app for the Veeam Backup & Replication 13.1 REST API. It runs on this laptop only
(http://127.0.0.1) and opens in your browser. Mock data works straight away, no server needed.

START
  Windows: double-click pulse.exe.
    The first time, Windows SmartScreen may say it "protected your PC", because this build
    isn't code-signed. Click "More info", then "Run anyway".
  macOS (Apple Silicon): this build isn't signed or notarized, so macOS blocks it until you
    clear the download quarantine once. In Terminal:
        xattr -dr com.apple.quarantine /path/to/vbr-pulse
    Then double-click "pulse" (it opens a Terminal window) or run ./pulse.

  The console window shows the address and must stay open. Close it (or press Ctrl+C) to stop.

CONNECT TO A LAB SERVER
  Open a terminal in this folder, then:
    pulse init                    creates your settings file and prints where it is
    (edit PULSE_VBR_URL and the PULSE_PROFILE_*_USER accounts in that file)
    pulse secret set <username>   stores each account's password in the OS keychain
    pulse preflight               checks the server, the accounts and the demo job
    pulse                         starts Pulse
  On Windows, use pulse.exe in place of pulse; on macOS, ./pulse.

  Portable use: a pulse.env file next to the executable is used before the per-user one.

MORE
  pulse --help, pulse config, and the project README.
"""


def platform_tag() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    return f"{system}-{arch}"


def check_tag() -> None:
    """In CI on a tag push, the tag must match the package version."""
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if tag != f"v{__version__}":
            raise SystemExit(f"Tag {tag} doesn't match src/pulse/__init__.py ({__version__}).")


def build() -> None:
    shutil.rmtree(APP_DIR, ignore_errors=True)
    subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(ROOT / "packaging" / "pulse.spec"),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(ROOT / "build" / "pyinstaller"),
        ],
        check=True,
        cwd=ROOT,
    )
    (APP_DIR / "README-FIRST.txt").write_text(
        README_FIRST.format(version=__version__), encoding="utf-8"
    )


def executable() -> Path:
    return APP_DIR / ("pulse.exe" if sys.platform == "win32" else "pulse")


def run(args: list[str], env: dict[str, str], timeout: float = 180) -> str:
    result = subprocess.run(  # noqa: S603 - our own build output
        [str(executable()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
        cwd=env["PULSE_SMOKE_CWD"],
    )
    if result.returncode != 0:
        raise SystemExit(f"`pulse {' '.join(args)}` failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def smoke() -> None:
    """Exercise the frozen build the way a presenter would, with no settings file."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "PULSE_SMOKE_CWD": tmp, "PYTHONIOENCODING": "utf-8"}
        env.pop("PULSE_CONFIG", None)

        assert run(["--version"], env).strip() == f"VBR Pulse {__version__}"
        print("  ✓ --version")

        config = run(["config"], env)
        backend = next(line for line in config.splitlines() if line.startswith("Keyring:"))
        assert "fail" not in backend.lower() and "null" not in backend.lower(), backend
        print(f"  ✓ config ({backend.split(':', 1)[1].strip()})")

        run(["scenarios", "happy", "incident", "--speed", "50"], env)
        print("  ✓ mock scenarios: happy, incident")

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(  # noqa: S603
            [str(executable()), "serve", "--mock", "--no-open", "--port", str(port)],
            env=env,
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(f"{base}/signin", timeout=2) as page:  # noqa: S310
                        body = page.read().decode()
                    break
                except OSError:
                    if time.monotonic() > deadline or server.poll() is not None:
                        output = server.stdout.read().decode() if server.stdout else ""
                        raise SystemExit(f"pulse serve didn't come up:\n{output}") from None
                    time.sleep(0.3)
            assert "Connect to a backup server" in body
            for path in (
                "/static/css/pulse.css",
                "/static/js/htmx.min.js",
                "/static/fonts/SourceSans3-latin.woff2",
                "/static/favicon.svg",
            ):
                with urllib.request.urlopen(base + path, timeout=5) as response:  # noqa: S310
                    assert response.status == 200, path
            print(f"  ✓ serve: sign-in page and static files on {base}")
        finally:
            server.terminate()
            server.wait(timeout=10)


def archive() -> Path:
    name = f"vbr-pulse-{__version__}-{platform_tag()}"
    target = DIST / f"{name}.zip"
    target.unlink(missing_ok=True)
    staged = DIST / name
    shutil.rmtree(staged, ignore_errors=True)
    APP_DIR.rename(staged)
    try:
        if sys.platform == "darwin":
            # ditto keeps symlinks and code signatures inside the bundle intact.
            subprocess.run(  # noqa: S603 - fixed argv
                ["ditto", "-c", "-k", "--keepParent", str(staged), str(target)],  # noqa: S607
                check=True,
            )
        else:
            shutil.make_archive(str(target.with_suffix("")), "zip", DIST, name)
    finally:
        staged.rename(APP_DIR)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-smoke", action="store_true", help="skip the smoke test")
    parser.add_argument("--no-build", action="store_true", help="reuse dist/vbr-pulse")
    args = parser.parse_args()

    check_tag()
    if not args.no_build:
        build()
    size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file()) / 1_048_576
    print(f"Built {APP_DIR} ({size:.0f} MB)")
    if not args.no_smoke:
        print("Smoke test:")
        smoke()
    target = archive()
    print(f"Release: {target} ({target.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    main()

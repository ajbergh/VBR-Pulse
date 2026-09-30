# PyInstaller spec for VBR Pulse release builds (Windows x64, macOS arm64).
# Build with: uv run python scripts/build_release.py
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is provided by PyInstaller

# Templates, CSS/JS, fonts and mock fixtures ship inside the package; the OpenAPI spec is
# copied to the same place the wheel puts it (pulse/openapi/), where spec.py looks first.
datas = collect_data_files("pulse")
datas += [(str(ROOT / "openapi" / "vbr-1.3-rev2.json"), "pulse/openapi")]
# keyring finds its OS backends through package metadata (entry points).
datas += copy_metadata("keyring") + copy_metadata("vbr-pulse")

hiddenimports = (
    collect_submodules("keyring.backends")
    + collect_submodules("uvicorn")
    + ["pulse.web.app", "pulse.mock.runner", "pulse.preflight"]
)

excludes = [
    "pulse.vbr.models.generated",  # 64k lines of models, never imported at runtime
    "tkinter",
    "pytest",
    "playwright",
    "datamodel_code_generator",
    "mypy",
    "ruff",
    "IPython",
    "uvloop",
    "httptools",
    "watchfiles",
    "websockets",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # one-folder build: starts faster, trips antivirus less
    name="pulse",
    console=True,  # the console window shows the URL and is how you stop Pulse
    upx=False,
    codesign_identity=None,  # macOS: PyInstaller applies an ad-hoc signature
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="vbr-pulse")

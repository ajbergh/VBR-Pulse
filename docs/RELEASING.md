# Releasing VBR Pulse

Releases are standalone builds for presenter laptops: **Windows x64** and **macOS on Apple
Silicon**. They bundle Python and every dependency, so the laptop needs nothing installed.

## Cut a release

1. Bump `__version__` in [src/pulse/__init__.py](../src/pulse/__init__.py) (the only place
   the version lives) and commit.
2. Tag and push: the tag must be `v` + that version.

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

3. The [Release workflow](../.github/workflows/release.yml) builds on a Windows runner and an
   Apple Silicon (`macos-14`) runner, smoke-tests each build, then creates the GitHub Release
   with both zips and `SHA256SUMS.txt`. It fails if the tag and `__version__` differ.

To try a build without publishing, run the workflow by hand (Actions → Release → Run workflow):
the zips appear as workflow artifacts.

## What the smoke test checks

`scripts/build_release.py` runs the frozen executable, not the source:

- `pulse --version` matches the package version;
- `pulse config` finds a real OS keyring backend (Windows Credential Manager / macOS Keychain);
- the `happy` and `incident` mock scenarios pass end to end (client, mock server, spec
  validation);
- `pulse serve` answers with the sign-in page, CSS, JS, fonts and favicon.

## Code signing (not set up yet)

The builds are unsigned, so users see a one-time warning (the steps are in README-FIRST.txt
inside each zip, and in the README):

- **Windows:** SmartScreen, "More info → Run anyway". Some antivirus products are wary of
  unsigned PyInstaller apps; the one-folder build (not a single self-extracting file) keeps
  that rare.
- **macOS:** Gatekeeper blocks unsigned, un-notarized downloads. Users clear the quarantine
  with `xattr -dr com.apple.quarantine <folder>`. PyInstaller applies an ad-hoc signature,
  which Apple Silicon requires to run at all, but that doesn't satisfy Gatekeeper.

When certificates are available, add them as repository secrets and extend the workflow:

- **Windows:** sign `pulse.exe` with `signtool sign /fd sha256 /tr <timestamp-url> /td sha256`
  (an Authenticode certificate; an EV certificate or Azure Trusted Signing builds SmartScreen
  reputation fastest).
- **macOS:** import the Developer ID Application certificate into a temporary keychain, set
  `codesign_identity` (and an entitlements file with
  `com.apple.security.cs.allow-unsigned-executable-memory` if needed) in
  [packaging/pulse.spec](../packaging/pulse.spec), then notarize the zip with
  `xcrun notarytool submit --wait` using an App Store Connect API key. A bare folder can't be
  stapled, so either wrap the build in a `.dmg` or `.app` and staple that, or rely on Gatekeeper
  checking the notarization online.

## Adding files or dependencies

- Data files inside `src/pulse/` (templates, static files, fixtures) are bundled
  automatically. Files outside the package need an entry in `datas` in `packaging/pulse.spec`.
- A dependency that loads plugins by name (entry points, `importlib`) may need
  `hiddenimports` or `copy_metadata` in the spec. The smoke test is there to catch this; run
  `uv run python scripts/build_release.py` locally after changing dependencies.

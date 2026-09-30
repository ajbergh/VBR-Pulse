# VBR Pulse — conventions for Claude Code

## Context
Demo app for the Veeam Backup & Replication 13.1 REST API. Plan: docs/VBR_Pulse_Implementation_Plan.md
(referred to as "PLAN"). Source of truth for API shapes: openapi/vbr-1.3-rev2.json. Never guess paths
or bodies.

## Hard rules
- Every VBR request goes through src/pulse/vbr/client.py. No ad-hoc httpx calls.
- Resolve endpoints by operationId (src/pulse/vbr/operations.py — generated, don't edit).
- Always send x-api-version: 1.3-rev2. Default port 443. Never reference 9419.
- TLS verification stays on unless PULSE_LAB_INSECURE_TLS=true.
- Never log, render or return passwords or tokens. Add a redaction test for any new field.
- Never auto-retry POST requests on 5xx or network errors. (Replaying once after a 401 is
  allowed: the server rejected the request before executing it.)
- v1 scope is read + trigger only. Do not implement create/edit/delete operations.

## Generated code
- `uv run python scripts/gen_operations.py` → src/pulse/vbr/operations.py
- `uv run python scripts/gen_models.py` → src/pulse/vbr/models/generated.py (~5 min; strips
  `discriminator=` hints Pydantic can't handle).
- The generated models take ~10 s to import. Never import them on the startup or request path;
  request bodies are validated with jsonschema against the spec (src/pulse/vbr/spec.py).
- openapi/vbr-1.3-rev2.json came from the public reference (scripts/extract_reference_spec.py).
  Replace it with the swagger.json exported from the lab server when available, then regenerate.

## Spec quirks worth knowing
- `grant_type` enum in TokenLoginSpec says `Password`/`Refresh_token`; the server and the
  description use lowercase `password`/`refresh_token`. We send lowercase.
- Polymorphic bodies (Quick Backup, malware scan) use base `oneOf` + subtype `allOf` cycles;
  spec.py rewrites them into discriminator if/then before validating.
- ~40 of Veeam's own request examples fail their schemas (lowercase enum values). Don't copy
  enum casing from examples; use the schema's enum.

## Style
- Python 3.12, type hints everywhere, ruff + mypy (strict) clean.
- UI: Jinja2 + htmx, CSS tokens from PLAN §7.2 only, sentence case copy,
  no all-caps labels, status always icon + word.

## Workflow
- Write or update tests first; run `uv run pytest` before reporting done.
- Checks: `uv run ruff check src tests scripts`, `uv run ruff format --check src tests scripts`,
  `uv run mypy`.
- For UI work, run Playwright screenshots and review them against PLAN §7.
- Keep mock fixtures valid: `uv run pytest tests/contract`.

# VBR Pulse

A small demo app that makes the Veeam Backup & Replication 13.1 REST API (`1.3-rev2`) visible:
every action in the UI is paired with an API inspector showing the exact request and response.

Plan and specification: [docs/VBR_Pulse_Implementation_Plan.md](docs/VBR_Pulse_Implementation_Plan.md).

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Repo layout, spec, generated operations + models | Done |
| 1 | `VbrClient`: auth, refresh, retry, paging, errors, inspector events, redaction | Done |
| 2 | Mock VBR server and scenarios | Next |
| 3–9 | Web layer, UI, jobs, repositories, incident flow, hardening, rehearsal | Planned |

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then edit PULSE_VBR_URL and the profile users
```

Secrets are never stored in `.env` in plain text by default. Put each account's password in the
OS keyring (Windows Credential Manager, macOS Keychain):

```bash
uv run keyring set vbr-pulse svc-pulse-ops
```

Resolution order per profile: OS keyring (`vbr-pulse` / username) → a literal
`PULSE_PROFILE_<NAME>_SECRET` value → a `vault://<service>/<name>` reference looked up in the keyring.

## Lab smoke test

Logs in with a profile, reads server info and job states, logs out, and prints the calls made:

```bash
uv run pulse smoke --profile ops
```

## Development

```bash
uv run pytest
uv run ruff check src tests scripts
uv run mypy
```

Regenerate code after replacing the spec:

```bash
uv run python scripts/gen_operations.py
uv run python scripts/gen_models.py
```

### About the OpenAPI spec

`openapi/vbr-1.3-rev2.json` was extracted from the public 1.3-rev2 reference with
`scripts/extract_reference_spec.py`. The plan prefers the `swagger.json` exported from your own
13.1 server (it ships in the Veeam Backup & Replication installation folder and is served by its
Swagger UI). Swap it in and regenerate when a lab server is available.

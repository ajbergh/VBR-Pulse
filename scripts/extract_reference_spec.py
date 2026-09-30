"""Extract the 1.3-rev2 OpenAPI spec embedded in the public Veeam REST reference.

The plan (§3) prefers exporting swagger.json from your own 13.1 server. Use this
script when no lab server is at hand: the public reference page embeds every
revision's spec in its Redocly state bundle, and this pulls out one revision.

    uv run python scripts/extract_reference_spec.py [--revision 1.3-rev2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REFERENCE_ROOT = "https://helpcenter.veeam.com/references/vbr/13/rest"
ROOT = Path(__file__).resolve().parent.parent


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https host
        return str(resp.read().decode("utf-8"))


def extract(revision: str) -> dict[str, object]:
    page = _get(f"{REFERENCE_ROOT}/{revision}/tag/SectionOverview")
    match = re.search(r'src="(/references/vbr/13/rest/redocly-state-[0-9a-f]+\.js)"', page)
    if not match:
        raise SystemExit("Could not find the Redocly state bundle on the reference page.")
    bundle = _get(f"https://helpcenter.veeam.com{match.group(1)}")

    # The bundle is `const __redoc_state = JSON.parse("<json string literal>");`
    start = bundle.index("JSON.parse(") + len("JSON.parse(")
    end = bundle.rindex('")') + 1
    state = json.loads(json.loads(bundle[start:end]))

    for version in state["definition"]["data"]["versions"]:
        if version["id"] == revision:
            spec: dict[str, object] = version["spec"]
            return spec
    available = [v["id"] for v in state["definition"]["data"]["versions"]]
    raise SystemExit(f"Revision {revision} not found. Available: {', '.join(available)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="1.3-rev2")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    spec = extract(args.revision)
    out: Path = args.out or ROOT / "openapi" / f"vbr-{args.revision}.json"
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths = spec.get("paths", {})
    print(f"Wrote {out} ({len(paths) if isinstance(paths, dict) else 0} paths)", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Write the OpenAPI schema to a file without starting a server.

`app.openapi()` builds the same document `GET /openapi.json` serves, so nothing
here needs a running instance, a port or an API key. That makes it usable from a
CI job, a pre-commit hook, or any OpenAPI tool that wants a file rather than a
URL -- client generation (`scripts/generate-client.sh`) being the obvious one.

    uv run python scripts/export-openapi.py              # -> ./openapi.json
    uv run python scripts/export-openapi.py --out /tmp/s.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Building the app reads settings, and the production defaults refuse to start
# without an API key. This process only introspects routes -- it never serves a
# request -- so it runs as a development instance.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("API_KEYS", "")
os.environ.setdefault("JOB_BACKEND", "memory")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("openapi.json"), help="Where to write the schema."
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact.")
    args = parser.parse_args()

    schema = app.openapi()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(schema, indent=args.indent or None, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    paths = len(schema.get("paths", {}))
    print(f"Wrote {args.out} ({paths} paths, OpenAPI {schema.get('openapi')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# Generate a typed client for this API from its own OpenAPI schema (§8).
#
#   ./scripts/generate-client.sh                      # typescript-fetch
#   ./scripts/generate-client.sh python
#   ./scripts/generate-client.sh typescript-axios go java ...
#
# Prerequisites: Node (for `npx`) and a JRE 11+ -- openapi-generator is a Java
# tool that npx only wraps. `npx` downloads the generator on first run, so the
# first invocation needs network access to the npm registry and to Maven Central.
# `openapitools.json` at the repo root pins the generator version, so two people
# running this get the same client; bump it there deliberately, not by accident.
#
# The schema is exported straight from the FastAPI app (`scripts/export-openapi.py`)
# rather than scraped from a running server: no port to pick, no instance to
# start, no API key. Set SCHEMA=<path-or-url> to generate from something else,
# e.g. a deployed instance's /openapi.json.
#
# Output lands in clients/<generator>/ and is gitignored. A generated client is a
# build artifact: committing one means re-reviewing thousands of machine-written
# lines on every schema change, and it goes stale the moment someone forgets to
# re-run this. Publish it from CI instead.
set -euo pipefail

cd "$(dirname "$0")/.."

GENERATORS=("$@")
if [ ${#GENERATORS[@]} -eq 0 ]; then
  GENERATORS=("typescript-fetch")
fi

command -v npx >/dev/null 2>&1 || { echo "npx not found: install Node.js" >&2; exit 1; }
command -v java >/dev/null 2>&1 || { echo "java not found: install a JRE 11+" >&2; exit 1; }

SCHEMA="${SCHEMA:-}"
if [ -z "$SCHEMA" ]; then
  SCHEMA="$(mktemp -t openapi-XXXXXX).json"
  trap 'rm -f "$SCHEMA"' EXIT
  echo "==> Exporting the OpenAPI schema"
  uv run python scripts/export-openapi.py --out "$SCHEMA"
fi

for generator in "${GENERATORS[@]}"; do
  out="clients/${generator}"
  echo "==> Generating ${generator} client into ${out}"
  rm -rf "$out"
  npx --yes @openapitools/openapi-generator-cli generate \
    -i "$SCHEMA" \
    -g "$generator" \
    -o "$out" \
    --additional-properties=npmName=unified-search-api-client,packageName=unified_search_api_client
done

echo
echo "Done. Generated: ${GENERATORS[*]} (under clients/)"

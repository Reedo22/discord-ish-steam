#!/usr/bin/env bash
# Run every test: daemon + Lua backend (python unittest) and the plugin frontend (node:test).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests "$@"
if command -v node >/dev/null 2>&1; then node --test tests/*.mjs
else echo "node not installed - skipping plugin (index.js) tests" >&2; fi

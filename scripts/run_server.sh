#!/usr/bin/env bash
# Start the PIR database server on loopback.
# Usage: scripts/run_server.sh [--catalog data/catalog/full] [--port 8700] [other pmr.server options]
#   e.g. scripts/run_server.sh --query-mode expanded --workers 2
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh
args=("$@")
has_catalog=0
for a in "${args[@]}"; do [ "$a" = "--catalog" ] && has_catalog=1; done
[ "$has_catalog" = 1 ] || args=(--catalog data/catalog/full "${args[@]}")
exec .venv/bin/python -m pmr.server "${args[@]}"

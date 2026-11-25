#!/usr/bin/env bash
# Start the trusted local client (holds the secret key) and its browser UI on loopback.
# Usage: scripts/run_client.sh [--server http://127.0.0.1:8700] [--port 8701] [other pmr.client options]
# Open the printed URL (http://127.0.0.1:8701/?t=<token>) in a browser on the same machine
# (from Windows with WSL2: same URL in the Windows browser; see README.md).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh
exec .venv/bin/python -m pmr.client --forbid-reads data/catalog "$@"

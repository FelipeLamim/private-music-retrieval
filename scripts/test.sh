#!/usr/bin/env bash
# Run the automated tests. Usage: scripts/test.sh [--all] [--browser] [extra pytest args]
#   default  : fast suite (pytest -m "not slow"), about 1 minute
#   --all    : also the slow full-length-track test
#   --browser: also the headless-browser playback test (needs scripts/setup.sh --with-browser-test)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh
all=0
browser=0
extra=()
for a in "$@"; do
  case "$a" in
    --all) all=1 ;;
    --browser) browser=1 ;;
    *) extra+=("$a") ;;
  esac
done
mkdir -p logs/tests
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log="logs/tests/pytest_${stamp}.txt"
cmd=(.venv/bin/python -m pytest -q -p no:cacheprovider)
if [ "$all" = 0 ]; then cmd+=(-m "not slow"); fi
if [ ${#extra[@]} -gt 0 ]; then cmd+=("${extra[@]}"); fi
rc=0
nice -n 10 "${cmd[@]}" > >(tee "$log") 2>&1 || rc=$?
echo "pytest exit code: $rc (log: $log)"
if [ "$browser" = 1 ]; then
  bash scripts/browser_test.sh || rc=1
fi
exit "$rc"

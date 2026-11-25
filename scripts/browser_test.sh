#!/usr/bin/env bash
# Headless-browser playback test of the private music-streaming UI, in one command.
#   1. starts a PIR server and a trusted client for data/catalog/mini and, for the cancel test,
#      for data/catalog/full; all bind 127.0.0.1 with ephemeral ports (--port 0 + --ready-file);
#      the clients run with --forbid-reads data/catalog;
#   2. runs tests/browser/run_browser_playback.py with .venv-browser (Playwright, Chrome Headless Shell);
#   3. stops every process it started and exits non-zero if a required test fails.
#
# Usage: scripts/browser_test.sh [--setup] [--no-full] [--keep-tmp] [--] [RUNNER_ARGS...]
#   --setup     run tests/browser/setup.sh first (venv, Node.js if needed, browser download)
#   --no-full   skip the full-catalog services (T5 then tries to cancel on the mini catalog)
#   --keep-tmp  keep .tmp/browser/run-* (its ready files contain the per-launch UI token)
#   RUNNER_ARGS are passed through, e.g. --playback-rate 4, --no-extras, --track 3
# Outputs: results/browser/browser_playback_results.json (+ runs/), logs/browser/*.
# Exit codes: 0 all required tests passed, 1 a required test failed, 2 setup/infrastructure error.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT" || exit 2
# shellcheck source=/dev/null
source ./env.sh
# shellcheck source=../tests/browser/browser_env.sh
source tests/browser/browser_env.sh

SETUP=0 FULL=1 KEEP=0
RUNNER_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --setup) SETUP=1 ;;
    --no-full) FULL=0 ;;
    --keep-tmp) KEEP=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    --) shift; RUNNER_ARGS+=("$@"); break ;;
    *) RUNNER_ARGS+=("$1") ;;
  esac
  shift
done

log() { printf '[browser-test] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 2; }

if [ "$SETUP" = 1 ]; then
  bash tests/browser/setup.sh || die "tests/browser/setup.sh failed"
fi
[ -x "$BROWSER_VENV/bin/python" ] || die "missing ${BROWSER_VENV#"$ROOT"/}: run scripts/browser_test.sh --setup"
"$BROWSER_VENV/bin/python" -c 'import playwright' 2>/dev/null || die "playwright is not installed in .venv-browser: run --setup"
pmr_select_node || die "Playwright's bundled Node.js cannot run here and ${PW_NODE_DIR#"$ROOT"/} is missing: run --setup"
compgen -G "$PLAYWRIGHT_BROWSERS_PATH/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell" >/dev/null \
  || die "no Chrome Headless Shell under ${PLAYWRIGHT_BROWSERS_PATH#"$ROOT"/}: run --setup"
[ -x .venv/bin/python ] || die "missing the main virtualenv .venv (PIR server and client)"
if [ -n "${PLAYWRIGHT_NODEJS_PATH:-}" ]; then
  log "Node.js for the Playwright driver: ${PLAYWRIGHT_NODEJS_PATH#"$ROOT"/} (PLAYWRIGHT_NODEJS_PATH)"
else
  log "Node.js for the Playwright driver: bundled with playwright"
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$ROOT/.tmp/browser/run-$RUN_ID"
LOG_DIR="$ROOT/logs/browser"
mkdir -p "$RUN_DIR/tmp" "$LOG_DIR" "$ROOT/results/browser" || die "cannot create run directories"

PIDS=() NAMES=()
RUNNER_PID=""
LAST_PID=""

stop_pid() {  # stop_pid PID NAME: SIGTERM, wait up to 10 s, then SIGKILL. Only PIDs started here.
  local pid=$1 name=$2 i
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null
  for i in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null
      log "stopped $name (pid $pid)"
      return 0
    fi
    sleep 0.2
  done
  kill -KILL "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  log "killed $name (pid $pid) after 10 s"
}

cleanup() {
  local rc=$? i pid f
  trap - EXIT INT TERM
  [ -n "$RUNNER_PID" ] && stop_pid "$RUNNER_PID" "test runner"
  # Browser processes of this run carry the run-unique TMPDIR in their --user-data-dir.
  for pid in $(pgrep -u "$(id -u)" -f -- "--user-data-dir=$RUN_DIR/tmp/" 2>/dev/null); do
    stop_pid "$pid" "leftover browser process"
  done
  for ((i = ${#PIDS[@]} - 1; i >= 0; i--)); do
    stop_pid "${PIDS[$i]}" "${NAMES[$i]}"
  done
  for f in "$RUN_DIR"/*.log; do  # service logs, with the per-launch UI token removed
    [ -f "$f" ] && sed -E 's/([?&]t=)[A-Za-z0-9_-]+/\1<redacted>/g' "$f" > "$LOG_DIR/service_$(basename "$f")"
  done
  if [ "$KEEP" = 0 ] && [[ "$RUN_DIR" == "$ROOT/.tmp/browser/run-"* ]]; then
    rm -rf -- "$RUN_DIR"
  fi
  log "exit code $rc"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_service() {  # start_service NAME CMD...  (sets LAST_PID)
  local name=$1
  shift
  nice -n 10 "$@" > "$RUN_DIR/$name.log" 2>&1 &
  LAST_PID=$!
  PIDS+=("$LAST_PID")
  NAMES+=("$name")
  log "started $name (pid $LAST_PID): ${*#"$ROOT"/}"
}

wait_ready() {  # wait_ready NAME READY_FILE PID TIMEOUT_S
  local name=$1 ready=$2 pid=$3 timeout=$4 n=0
  while [ ! -s "$ready" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      log "$name exited before it was ready; last log lines:"
      tail -20 "$RUN_DIR/$name.log" | sed -E 's/([?&]t=)[A-Za-z0-9_-]+/\1<redacted>/g; s/^/    /'
      return 1
    fi
    if [ "$n" -ge $((timeout * 5)) ]; then
      log "$name was not ready after ${timeout} s"
      return 1
    fi
    sleep 0.2
    n=$((n + 1))
  done
}

port_of() { "$BROWSER_VENV/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1]))["listen"]["port"])' "$1"; }

CATALOGS=(mini)
[ "$FULL" = 1 ] && CATALOGS+=(full)
declare -A PIR_PID CLI_PID
for c in "${CATALOGS[@]}"; do
  start_service "pir_$c" .venv/bin/python -m pmr.server --catalog "data/catalog/$c" --port 0 \
    --ready-file "$RUN_DIR/pir_${c}_ready.json"
  PIR_PID[$c]=$LAST_PID
done
for c in "${CATALOGS[@]}"; do
  wait_ready "pir_$c" "$RUN_DIR/pir_${c}_ready.json" "${PIR_PID[$c]}" 180 || die "PIR server ($c) did not start"
  port="$(port_of "$RUN_DIR/pir_${c}_ready.json")" || die "cannot read the PIR ready file ($c)"
  log "PIR server ($c) ready on 127.0.0.1:$port"
  start_service "client_$c" .venv/bin/python -m pmr.client --server "http://127.0.0.1:$port" --port 0 \
    --forbid-reads data/catalog --ready-file "$RUN_DIR/client_${c}_ready.json"
  CLI_PID[$c]=$LAST_PID
done
for c in "${CATALOGS[@]}"; do
  wait_ready "client_$c" "$RUN_DIR/client_${c}_ready.json" "${CLI_PID[$c]}" 120 || die "trusted client ($c) did not start"
  log "trusted client ($c) ready on 127.0.0.1:$(port_of "$RUN_DIR/client_${c}_ready.json")"
done

ARGS=(--mini-client "$RUN_DIR/client_mini_ready.json" --mini-pir "$RUN_DIR/pir_mini_ready.json")
if [ "$FULL" = 1 ]; then
  ARGS+=(--full-client "$RUN_DIR/client_full_ready.json" --full-pir "$RUN_DIR/pir_full_ready.json")
fi
log "running the browser tests (log: logs/browser/runner.log)"
TMPDIR="$RUN_DIR/tmp" TMP="$RUN_DIR/tmp" TEMP="$RUN_DIR/tmp" \
  nice -n 10 "$BROWSER_VENV/bin/python" tests/browser/run_browser_playback.py "${ARGS[@]}" \
  ${RUNNER_ARGS[@]+"${RUNNER_ARGS[@]}"} > >(tee "$LOG_DIR/runner.log") 2>&1 &
RUNNER_PID=$!
wait "$RUNNER_PID"
rc=$?
RUNNER_PID=""

# Post-conditions on the services themselves.
for c in "${CATALOGS[@]}"; do
  for name in "pir_$c" "client_$c"; do
    pid=$([ "$name" = "pir_$c" ] && echo "${PIR_PID[$c]}" || echo "${CLI_PID[$c]}")
    if ! kill -0 "$pid" 2>/dev/null; then
      log "FAIL: $name (pid $pid) is no longer running"
      [ "$rc" = 0 ] && rc=1
    fi
  done
  if grep -q "trusted client attempted to open server audio" "$RUN_DIR/client_$c.log"; then
    log "FAIL: the --forbid-reads guard fired in client_$c"
    [ "$rc" = 0 ] && rc=1
  fi
done
case "$rc" in
  0) log "PASS: all required browser tests passed" ;;
  1) log "FAIL: at least one required browser test failed" ;;
  *) log "ERROR: the test run did not complete (runner exit $rc)"; [ "$rc" -gt 2 ] && rc=2 ;;
esac
log "results: results/browser/browser_playback_results.json; screenshots and logs: logs/browser/"
exit "$rc"

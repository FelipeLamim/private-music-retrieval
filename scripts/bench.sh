#!/usr/bin/env bash
# Run experiment plans sequentially (never in parallel, so they do not disturb each other's timings).
# Usage: scripts/bench.sh [PLAN ...]      (default: all plans in experiments/plans/e*.json)
#        RUN_TAG=mytag scripts/bench.sh e1_main
# Results: results/experiments/<UTC>_<plan>/ ; console log: logs/bench_<UTC>.log
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh
plans=("$@")
if [ ${#plans[@]} -eq 0 ]; then
  plans=()
  for f in experiments/plans/e*.json; do plans+=("$(basename "$f" .json)"); done
fi
mkdir -p logs results/experiments
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log="logs/bench_${stamp}.log"
echo "bench start $stamp plans: ${plans[*]}" | tee -a "$log"
for p in "${plans[@]}"; do
  f="experiments/plans/$p.json"
  [ -f "$f" ] || { echo "missing plan $f" | tee -a "$log"; exit 2; }
  run_id="$(date -u +%Y%m%dT%H%M%SZ)_${p}${RUN_TAG:+_$RUN_TAG}"
  echo "== $p -> results/experiments/$run_id" | tee -a "$log"
  nice -n 10 .venv/bin/python -m pmr.experiments.runner --plan "$f" --out results/experiments --run-id "$run_id" 2>&1 | tee -a "$log"
done
echo "bench done $(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$log"

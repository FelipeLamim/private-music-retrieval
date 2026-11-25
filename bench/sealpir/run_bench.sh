#!/usr/bin/env bash
# Run the SealPIR benchmark matrix. One process per configuration, sequential, nice -n 10,
# address space capped at SP_VMEM_KB (< 4 GiB). Raw JSONL in results/sealpir/raw/,
# per-run stderr and rusage in results/sealpir/logs/.
#   usage: run_bench.sh [all|calib|probe|A|B|C|I]   (default: all)
#
# Builds:   default      SEAL 4.0.0 with default options (+ GCC 7 ICE patch on this machine)
#           avoidbranch  same, but SEAL_AVOID_BRANCHING=ON (sensitivity run, see build_seal.sh)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

WHAT="${1:-all}"
RAW="$SP_RESULTS/raw"
mkdir -p "$RAW"
BIN_default="$SP_BUILD/sealpir_bench/sealpir_bench"
BIN_avoidbranch="$SP_BUILD/sealpir_bench-avoidbranch/sealpir_bench"
OPS_default="$SP_BUILD/sealpir_bench/seal_ops_bench"
OPS_avoidbranch="$SP_BUILD/sealpir_bench-avoidbranch/seal_ops_bench"
for b in "$BIN_default" "$BIN_avoidbranch"; do [[ -x "$b" ]] || { echo "missing $b (run build_bench.sh)"; exit 1; }; done

# run LABEL BUILD [driver args...]
run() {
  local label="$1" build="$2"; shift 2
  local bin_var="BIN_${build}"
  rm -f "$RAW/$label.jsonl"
  echo "[$(date -u +%T)] $label ($build): $*"
  ( ulimit -v "$SP_VMEM_KB"
    $SP_NICE python3 "$SP_BENCH_DIR/rss_run.py" "$SP_LOGS/run_$label.rusage.json" -- \
      "${!bin_var}" --label "$label" --out "$RAW/$label.jsonl" "$@" ) \
    > "$SP_LOGS/run_$label.stdout.log" 2> "$SP_LOGS/run_$label.stderr.log" && rc=0 || rc=$?
  echo "    exit=$rc  $(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('wall_s=%s max_rss_MiB=%.1f' % (d['wall_s'], d['max_rss_kib']/1024))" "$SP_LOGS/run_$label.rusage.json")"
}

if [[ "$WHAT" == all || "$WHAT" == calib ]]; then
  out="$SP_RESULTS/calibration_seal_ops.jsonl"; : > "$out"
  echo "[$(date -u +%T)] calibration (300 reps per op)"
  ( ulimit -v "$SP_VMEM_KB"
    $SP_NICE "$OPS_default" 300 | sed 's/^{/{"build":"seal-4.0.0-default",/' >> "$out"
    $SP_NICE "$OPS_avoidbranch" 300 | sed 's/^{/{"build":"seal-4.0.0-avoidbranch",/' >> "$out"
    $SP_NICE python3 "$SP_BENCH_DIR/seal_ops_bench.py" 300 | sed 's/^{/{"build":"tenseal-0.3.14-wheel",/' >> "$out" )
  wc -l "$out"
fi

if [[ "$WHAT" == all || "$WHAT" == probe ]]; then
  # SealPIR's element-size limit (expected: assertion failures above N*logt/8 = 10,240 bytes).
  run P_size10240_R10 default --mode probe --items 10 --size 10240
  run P_size10241_R10 default --mode probe --items 10 --size 10241
  run P_size20480_R10 default --mode probe --items 10 --size 20480
  run P_size5760000_R10 default --mode probe --items 10 --size 5760000
  run P_size20480_R2_nobatch_db default --mode probe --items 2 --size 20480 --batching 0 --probe-db 1
  run P_size5760000_R2_nobatch_db default --mode probe --items 2 --size 5760000 --batching 0 --probe-db 1
fi

for build in default avoidbranch; do
  sfx=""; [[ "$build" == avoidbranch ]] && sfx="_ab"
  if [[ "$WHAT" == all || "$WHAT" == A ]]; then
    for R in 10 20 40 100; do
      for S in 8192 10240; do
        run "A_R${R}_S${S}${sfx}" "$build" --mode block --items "$R" --size "$S" --d 1 --warmup 1 --trials 10
      done
    done
    if [[ "$build" == default ]]; then
      run A_R10_S8192_pubkey default --mode block --items 10 --size 8192 --d 1 --symmetric 0 --warmup 1 --trials 10
    fi
  fi
  if [[ "$WHAT" == all || "$WHAT" == C ]]; then
    run "C_R100_S8192_d2${sfx}" "$build" --mode block --items 100 --size 8192 --d 2 --warmup 1 --trials 10
  fi
  if [[ "$WHAT" == all || "$WHAT" == B ]]; then
    for R in 10 20; do
      for V in api reuse; do
        run "B_R${R}_${V}${sfx}" "$build" --mode track --variant "$V" --items "$R" \
            --track-bytes 5760000 --chunk 10240 --warmup 1 --trials 5
      done
    done
  fi
done

if [[ "$WHAT" == all || "$WHAT" == I ]]; then
  run I_R10_S10240_K40 default --mode instances --items 10 --size 10240 --instances 40
  run I_R20_S10240_K40 default --mode instances --items 20 --size 10240 --instances 40
fi
echo "done: $RAW"

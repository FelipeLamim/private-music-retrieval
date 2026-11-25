#!/usr/bin/env bash
# Build bench/sealpir/sealpir_bench against SEAL 4.0.0 (third_party/install) and the SealPIR sources.
# Flags: -O3 without -DNDEBUG (SealPIR's own parameter checks are assert()s; keep them active).
# SP_SEAL_AVOID_BRANCHING=ON links the SEAL variant built with SEAL_AVOID_BRANCHING=ON.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [[ "${SP_SEAL_AVOID_BRANCHING:-OFF}" == ON ]]; then SFX="-avoidbranch"; else SFX=""; fi
SP_SEAL_PREFIX="${SP_SEAL_PREFIX}${SFX}"
BUILD_DIR="$SP_BUILD/sealpir_bench${SFX}"
(
  set -x
  $SP_NICE "$SP_CMAKE" -S "$SP_BENCH_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE=-O3 \
    -DCMAKE_CXX_COMPILER=g++ \
    -DSEAL_DIR="$SP_SEAL_PREFIX/lib64/cmake/SEAL-4.0" \
    -DSEALPIR_SOURCE_DIR="$SP_SEALPIR_SRC" -DSEALPIR_COMMIT="$SP_SEALPIR_COMMIT" \
    "${SP_CMAKE_SAFE_FLAGS[@]}"
  $SP_NICE "$SP_CMAKE" --build "$BUILD_DIR" -j "$SP_JOBS" -- VERBOSE=1
) > "$SP_LOGS/bench_build${SFX}.log" 2>&1 || { tail -n 40 "$SP_LOGS/bench_build${SFX}.log"; exit 1; }
if grep -h "git is disabled" "$SP_LOGS/bench_build${SFX}.log"; then
  echo "ERROR: a build step tried to run Git" >&2; exit 1
fi
grep -E 'warning' "$SP_LOGS/bench_build${SFX}.log" | sort -u | head -n 20 || true
ls -la "$BUILD_DIR/sealpir_bench"

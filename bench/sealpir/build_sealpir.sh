#!/usr/bin/env bash
# Build Microsoft SealPIR (master commit pinned in common.sh) against the project-local SEAL 4.0.0,
# run its example program bin/main once, and run its unit tests with ctest.
#
# Upstream instructions are `cmake . && make` (empty CMAKE_BUILD_TYPE, i.e. no optimisation flags).
# We build out of source with -O3 and WITHOUT -DNDEBUG: SealPIR's tests call the test body inside
# assert(...), so NDEBUG would silently skip them, and its parameter checks are asserts as well.
# SealPIR's tests are plain executables (no GoogleTest needed). Upstream CMake writes executables to
# <source>/bin (CMAKE_RUNTIME_OUTPUT_DIRECTORY = ${CMAKE_SOURCE_DIR}/bin).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

[[ -d "$SP_SEALPIR_SRC" ]] || tar -xzf "$SP_ARCHIVES/SealPIR-${SP_SEALPIR_COMMIT}.tar.gz" -C "$SP_SRC"
BUILD_DIR="$SP_BUILD/sealpir-${SP_SEALPIR_COMMIT}"
mkdir -p "$BUILD_DIR"

(
  echo "# $(date -u +%FT%TZ) configure SealPIR ${SP_SEALPIR_COMMIT}"
  set -x
  $SP_NICE "$SP_CMAKE" -S "$SP_SEALPIR_SRC" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE=-O3 \
    -DCMAKE_CXX_COMPILER=g++ \
    -DSEAL_DIR="$SP_SEAL_PREFIX/lib64/cmake/SEAL-4.0" \
    "${SP_CMAKE_SAFE_FLAGS[@]}"
) > "$SP_LOGS/sealpir_configure.log" 2>&1

(
  echo "# $(date -u +%FT%TZ) build SealPIR (-j${SP_JOBS})"
  $SP_NICE "$SP_CMAKE" --build "$BUILD_DIR" -j "$SP_JOBS" -- VERBOSE=1
) > "$SP_LOGS/sealpir_build.log" 2>&1

if grep -h "git is disabled" "$SP_LOGS"/sealpir_{configure,build}.log; then
  echo "ERROR: a build step tried to run Git" >&2; exit 1
fi
ls -la "$SP_SEALPIR_SRC/bin"

# bin/main once (its defaults: 2^16 items x 1024 B, N=4096, logt=20, d=2, symmetric, batching,
# recursive mod switching). Output and peak RSS (GNU time) are saved.
if [[ "${SP_SKIP_MAIN:-0}" != 1 ]]; then
  ( ulimit -v "$SP_VMEM_KB"; cd "$SP_SEALPIR_SRC" && \
    $SP_NICE python3 "$SP_BENCH_DIR/rss_run.py" "$SP_RESULTS/sealpir_main_rusage.json" -- ./bin/main ) \
    > "$SP_RESULTS/sealpir_main_output.txt" 2>&1 && rc=0 || rc=$?
  echo "bin/main exit code: $rc"
  grep -E '^Main: (PIR result|PIRServer|PIRClient|Query size|Reply)' "$SP_RESULTS/sealpir_main_output.txt" || true
  cat "$SP_RESULTS/sealpir_main_rusage.json" || true
fi

# Unit tests: sequential, each process capped below 4 GiB of address space.
if [[ "${SP_SKIP_SEALPIR_TESTS:-0}" != 1 ]]; then
  ( ulimit -v "$SP_VMEM_KB"; cd "$BUILD_DIR" && $SP_NICE "$SP_CTEST" --output-on-failure --timeout 1800 -j1 ) \
    > "$SP_LOGS/sealpir_ctest.log" 2>&1 && rc=0 || rc=$?
  grep -E 'Test +#|tests passed|tests failed|Total Test' "$SP_LOGS/sealpir_ctest.log" || true
  echo "ctest exit code: $rc"
fi

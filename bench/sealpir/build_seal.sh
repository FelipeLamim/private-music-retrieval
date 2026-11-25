#!/usr/bin/env bash
# Build and install Microsoft SEAL 4.0.0 (Release, static) into third_party/install/seal-4.0.0,
# then build and run SEAL's own unit tests (sealtest) as a check of this toolchain.
#
# Dependencies come only from source archives (see fetch_sources.sh):
#   - Zstandard 1.4.5 (the commit SEAL 4.0.0 pins) via FETCHCONTENT_SOURCE_DIR_ZSTD,
#     so serialized sizes use SEAL's default zstd compression, as in TenSEAL's SEAL 4.1.1;
#   - GoogleTest 1.11.0 (pinned by SEAL 4.0.0) via FETCHCONTENT_SOURCE_DIR_GOOGLETEST, tests only.
# FetchContent is fully disconnected: no Git and no network access during configure/build.
# MSGSL, ZLIB and Intel HEXL are disabled (HEXL is also OFF in TenSEAL 0.3.14's SEAL build).
#
# GCC 7 workaround: GCC 7.3.1 (this machine) hits an internal compiler error in
# native/src/seal/util/rns.cpp:1232 (tsubst_copy, cp/pt.c:14588). With GCC < 8 the script
# applies patches/seal-4.0.0-gcc7-ice-rns.patch, which only drops a `const` qualifier from a
# local variable. Set SP_SEAL_GCC7_PATCH=always|never to override the automatic choice.
#
# SP_SEAL_AVOID_BRANCHING=ON builds a second variant (SEAL's official SEAL_AVOID_BRANCHING option,
# branch-free SEAL_COND_SELECT) into third_party/install/seal-4.0.0-avoidbranch. Reason: GCC 7.3
# compiles SEAL's conditional subtraction in add_poly_coeffmod to a data-dependent branch
# (cmp/ja), which makes ciphertext additions ~6x slower than in TenSEAL's SEAL 4.1.1 wheel
# (see results/sealpir/calibration_seal_ops.jsonl).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
AVOID_BRANCHING="${SP_SEAL_AVOID_BRANCHING:-OFF}"
if [[ "$AVOID_BRANCHING" == ON ]]; then
  SP_SEAL_PREFIX="${SP_SEAL_PREFIX}-avoidbranch"; VARIANT_SUFFIX="-avoidbranch"
else
  VARIANT_SUFFIX=""
fi

[[ -d "$SP_SEAL_SRC" ]] || tar -xzf "$SP_ARCHIVES/SEAL-${SP_SEAL_VERSION}.tar.gz" -C "$SP_SRC"
[[ -d "$SP_ZSTD_SRC" ]] || tar -xzf "$SP_ARCHIVES/zstd-${SP_ZSTD_COMMIT}.tar.gz" -C "$SP_SRC"
[[ -d "$SP_GTEST_SRC" ]] || tar -xzf "$SP_ARCHIVES/googletest-${SP_GTEST_COMMIT}.tar.gz" -C "$SP_SRC"

PATCH="$SP_BENCH_DIR/patches/seal-4.0.0-gcc7-ice-rns.patch"
GCC_MAJOR="$(g++ -dumpversion | cut -d. -f1)"
mode="${SP_SEAL_GCC7_PATCH:-auto}"
want_patch=0
if [[ "$mode" == always ]] || { [[ "$mode" == auto ]] && (( GCC_MAJOR < 8 )); }; then want_patch=1; fi
if patch -d "$SP_SEAL_SRC" -p1 --dry-run --reverse --force < "$PATCH" > /dev/null 2>&1; then
  patched=1
else
  patched=0
fi
if (( want_patch && !patched )); then
  patch -d "$SP_SEAL_SRC" -p1 --forward < "$PATCH"; patched=1
elif (( !want_patch && patched )); then
  patch -d "$SP_SEAL_SRC" -p1 --reverse < "$PATCH"; patched=0
fi
echo "g++ major version: $GCC_MAJOR; GCC 7 workaround patch applied: $patched" | tee "$SP_LOGS/seal_patch_state.txt"

BUILD_DIR="$SP_BUILD/seal-${SP_SEAL_VERSION}${VARIANT_SUFFIX}"
# SEAL builds zstd in "<zstd source>/../zstd-build", so each variant gets its own zstd source copy.
if [[ -n "$VARIANT_SUFFIX" ]]; then
  ZSTD_SRC_DIR="$SP_SRC/deps${VARIANT_SUFFIX}/zstd-${SP_ZSTD_COMMIT}"
  [[ -d "$ZSTD_SRC_DIR" ]] || { mkdir -p "$SP_SRC/deps${VARIANT_SUFFIX}"; tar -xzf "$SP_ARCHIVES/zstd-${SP_ZSTD_COMMIT}.tar.gz" -C "$SP_SRC/deps${VARIANT_SUFFIX}"; }
else
  ZSTD_SRC_DIR="$SP_ZSTD_SRC"
fi
LOGP="$SP_LOGS/seal${VARIANT_SUFFIX}"
mkdir -p "$BUILD_DIR"

(
  echo "# $(date -u +%FT%TZ) configure SEAL ${SP_SEAL_VERSION}"
  set -x
  $SP_NICE "$SP_CMAKE" -S "$SP_SEAL_SRC" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
    -DCMAKE_INSTALL_PREFIX="$SP_SEAL_PREFIX" \
    -DBUILD_SHARED_LIBS=OFF \
    -DSEAL_BUILD_DEPS=ON \
    -DSEAL_USE_ZSTD=ON -DSEAL_USE_ZLIB=OFF -DSEAL_USE_MSGSL=OFF -DSEAL_USE_INTEL_HEXL=OFF \
    -DSEAL_AVOID_BRANCHING="$AVOID_BRANCHING" \
    -DSEAL_BUILD_TESTS=ON -DSEAL_BUILD_EXAMPLES=OFF -DSEAL_BUILD_BENCH=OFF -DSEAL_BUILD_SEAL_C=OFF \
    -DFETCHCONTENT_SOURCE_DIR_ZSTD="$ZSTD_SRC_DIR" \
    -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST="$SP_GTEST_SRC" \
    "${SP_CMAKE_SAFE_FLAGS[@]}"
) > "${LOGP}_configure.log" 2>&1

{
  echo "# $(date -u +%FT%TZ) build SEAL ${SP_SEAL_VERSION} + sealtest (-j${SP_JOBS})"
  $SP_NICE "$SP_CMAKE" --build "$BUILD_DIR" -j "$SP_JOBS"
} > "${LOGP}_build.log" 2>&1

{
  echo "# $(date -u +%FT%TZ) install SEAL ${SP_SEAL_VERSION}"
  "$SP_CMAKE" --install "$BUILD_DIR"
} > "${LOGP}_install.log" 2>&1

# Guard: the git stub prints this line if anything tried to run Git.
if grep -h "git is disabled" "${LOGP}"_{configure,build,install}.log; then
  echo "ERROR: a build step tried to run Git" >&2; exit 1
fi
echo "SEAL ${SP_SEAL_VERSION} installed in $SP_SEAL_PREFIX"
grep -E "SEAL_USE_(ZSTD|ZLIB|MSGSL|INTEL_HEXL|INTRIN|CXX17)|SEAL_AVOID_BRANCHING|SEAL_BUILD_DEPS|The CXX compiler" "${LOGP}_configure.log" | sort -u || true

# SEAL's own unit tests (single process, lowered priority, memory-capped).
if [[ "${SP_SKIP_SEAL_TESTS:-0}" != 1 ]]; then
  ( ulimit -v "$SP_VMEM_KB"; cd "$BUILD_DIR" && $SP_NICE ./bin/sealtest --gtest_brief=1 ) \
    > "${LOGP}_tests.log" 2>&1 && rc=0 || rc=$?
  tail -n 5 "${LOGP}_tests.log"
  echo "sealtest exit code: $rc"
  exit "$rc"
fi

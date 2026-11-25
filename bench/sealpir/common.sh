# Shared settings for the SealPIR assessment scripts. Source it from bash:
#     source "$(dirname "$0")/common.sh"
# Tools, environments and caches stay inside the project root (see README.md).
set -euo pipefail

SP_BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -L)"
SP_ROOT="$(cd "$SP_BENCH_DIR/../.." && pwd -L)"
# shellcheck disable=SC1091
source "$SP_ROOT/env.sh"

# Pinned inputs (URL + SHA-256 recorded in third_party/archives/SOURCES.tsv).
SP_CMAKE_VERSION="3.31.12"
SP_SEAL_VERSION="4.0.0"
SP_ZSTD_COMMIT="b706286adbba780006a47ef92df0ad7a785666b6"   # zstd 1.4.5, as pinned by SEAL 4.0.0
SP_SEALPIR_COMMIT="cf55f10be7bffba4e78d5ba32764284ac828d6cd" # SealPIR master on 2026-11-21
SP_GTEST_COMMIT="e2239ee6043f73722e7aa812a459f54a28552929"   # GoogleTest 1.11.0, as pinned by SEAL 4.0.0 (SEAL tests only)

SP_ARCHIVES="$SP_ROOT/third_party/archives"
SP_SRC="$SP_ROOT/third_party/src"
SP_BUILD="$SP_ROOT/third_party/build"
SP_INSTALL="$SP_ROOT/third_party/install"
SP_RESULTS="$SP_ROOT/results/sealpir"
SP_LOGS="$SP_RESULTS/logs"

SP_CMAKE_HOME="$SP_ROOT/.tools/cmake/cmake-${SP_CMAKE_VERSION}-linux-x86_64"
SP_CMAKE="$SP_CMAKE_HOME/bin/cmake"
SP_CTEST="$SP_CMAKE_HOME/bin/ctest"

SP_SEAL_SRC="$SP_SRC/SEAL-${SP_SEAL_VERSION}"
SP_ZSTD_SRC="$SP_SRC/zstd-${SP_ZSTD_COMMIT}"
SP_SEAL_PREFIX="$SP_INSTALL/seal-${SP_SEAL_VERSION}"
SP_SEALPIR_SRC="$SP_SRC/SealPIR-${SP_SEALPIR_COMMIT}"
SP_GTEST_SRC="$SP_SRC/googletest-${SP_GTEST_COMMIT}"

# Shared-machine limits: at most 4 build jobs, lowered priority,
# and an address-space cap (KiB) that keeps every benchmark process below 4 GiB.
SP_JOBS=4
SP_NICE="nice -n 10"
SP_VMEM_KB=3900000

# CMake flags that keep CMake away from Git, the network and the user package registry.
SP_CMAKE_SAFE_FLAGS=(
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON
  -DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON
  -DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF
  -DCMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY=ON
  -DCMAKE_FIND_PACKAGE_NO_SYSTEM_PACKAGE_REGISTRY=ON
)

mkdir -p "$SP_ARCHIVES" "$SP_SRC" "$SP_BUILD" "$SP_INSTALL" "$SP_RESULTS" "$SP_LOGS"

#!/usr/bin/env bash
# Install the verified CMake binary release into .tools/cmake (project-local; no system changes).
# Run fetch_sources.sh first: it downloads the tarball and checks it against Kitware's
# cmake-<ver>-SHA-256.txt (and the GPG signature of that file when gpg is available).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TARBALL="$SP_ARCHIVES/cmake-${SP_CMAKE_VERSION}-linux-x86_64.tar.gz"
( cd "$SP_ARCHIVES" && grep " cmake-${SP_CMAKE_VERSION}-linux-x86_64.tar.gz\$" "cmake-${SP_CMAKE_VERSION}-SHA-256.txt" | sha256sum -c - )
mkdir -p "$SP_ROOT/.tools/cmake"
[[ -x "$SP_CMAKE" ]] || tar -xzf "$TARBALL" -C "$SP_ROOT/.tools/cmake"
"$SP_CMAKE" --version | head -n 1

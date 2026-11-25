#!/usr/bin/env bash
# Download (if missing) and verify every archive used by the SealPIR assessment.
# No Git: only HTTPS release/source archives. Pinned SHA-256 values were recorded
# on 2026-11-21. GitHub "archive/<ref>.tar.gz" tarballs are generated on the fly;
# GitHub keeps them stable in practice, but a mismatch is reported, never ignored.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

MANIFEST="$SP_ARCHIVES/SOURCES.tsv"
cat > "$MANIFEST" <<EOF
# file	url	sha256
cmake-${SP_CMAKE_VERSION}-linux-x86_64.tar.gz	https://github.com/Kitware/CMake/releases/download/v${SP_CMAKE_VERSION}/cmake-${SP_CMAKE_VERSION}-linux-x86_64.tar.gz	0dc2e9a6860f06bf10bd8fadc03e35d9eeb4df46e33763a7e480e987758f385c
cmake-${SP_CMAKE_VERSION}-SHA-256.txt	https://github.com/Kitware/CMake/releases/download/v${SP_CMAKE_VERSION}/cmake-${SP_CMAKE_VERSION}-SHA-256.txt	159eb3b123ba8c8403a154c9d3389432276bf3bd21b274f0396fe17a6a7a7d9d
cmake-${SP_CMAKE_VERSION}-SHA-256.txt.asc	https://github.com/Kitware/CMake/releases/download/v${SP_CMAKE_VERSION}/cmake-${SP_CMAKE_VERSION}-SHA-256.txt.asc	af2c668941be2f3ec852f42b1e825e14b05dbc5d11c1f1a91e3986356672ded2
SEAL-${SP_SEAL_VERSION}.tar.gz	https://github.com/microsoft/SEAL/archive/refs/tags/v${SP_SEAL_VERSION}.tar.gz	616653498ba8f3e0cd23abef1d451c6e161a63bd88922f43de4b3595348b5c7e
zstd-${SP_ZSTD_COMMIT}.tar.gz	https://github.com/facebook/zstd/archive/${SP_ZSTD_COMMIT}.tar.gz	c05687566bf68a5182d11a3e3498060f5b4d4243cf3c7e72c02d3cd4dadaeda7
SealPIR-${SP_SEALPIR_COMMIT}.tar.gz	https://github.com/microsoft/SealPIR/archive/${SP_SEALPIR_COMMIT}.tar.gz	4dfe1a86c4988adfcb06b54d8d0185d4d2b44a046550ed5a17bbdc3471cc20c0
googletest-${SP_GTEST_COMMIT}.tar.gz	https://github.com/google/googletest/archive/${SP_GTEST_COMMIT}.tar.gz	2765fbfaa978e90d1c8477bc2f2b9705be65b1b562bb215dbfaa76567d508fe1
EOF

status=0
while IFS=$'\t' read -r file url sha; do
  [[ -z "$file" || "$file" == \#* ]] && continue
  path="$SP_ARCHIVES/$file"
  if [[ ! -f "$path" ]]; then
    echo "download: $url"
    curl -sSfL --proto '=https' --tlsv1.2 -o "$path.part" "$url"
    mv "$path.part" "$path"
  fi
  if echo "$sha  $path" | sha256sum -c --status -; then
    echo "OK       $file  $sha"
  else
    echo "MISMATCH $file  expected $sha  got $(sha256sum "$path" | cut -d' ' -f1)" >&2
    status=1
  fi
done < "$MANIFEST"

# CMake: also check the tarball against Kitware's own checksum file.
( cd "$SP_ARCHIVES" && grep " cmake-${SP_CMAKE_VERSION}-linux-x86_64.tar.gz\$" "cmake-${SP_CMAKE_VERSION}-SHA-256.txt" | sha256sum -c - ) || status=1

# Optional: GPG signature of Kitware's checksum file (project-local keyring).
if command -v gpg >/dev/null 2>&1 && [[ -f "$SP_ROOT/third_party/meta/kitware_signer_2D2CEF1034921684.asc" ]]; then
  export GNUPGHOME="$SP_ROOT/third_party/meta/gnupg"
  mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"
  gpg --batch --quiet --import "$SP_ROOT/third_party/meta/kitware_signer_2D2CEF1034921684.asc" 2>/dev/null || true
  gpg --batch --verify "$SP_ARCHIVES/cmake-${SP_CMAKE_VERSION}-SHA-256.txt.asc" \
      "$SP_ARCHIVES/cmake-${SP_CMAKE_VERSION}-SHA-256.txt" 2>&1 | grep -E 'Good signature|BAD|fingerprint' || true
fi
exit $status

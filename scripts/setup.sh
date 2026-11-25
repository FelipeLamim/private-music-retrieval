#!/usr/bin/env bash
# Project-local setup: uv (pinned, hash-checked) -> CPython 3.10 (managed by uv, inside .tools/)
# -> .venv with hash-pinned wheels from requirements.lock -> catalogs (if missing) -> self-check.
# Nothing is installed outside this directory. Safe to re-run.
#
# Usage: scripts/setup.sh [--no-data] [--with-browser-test]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh

UV_VERSION="0.13.9"
UV_WHEEL_URL="https://files.pythonhosted.org/packages/3d/aa/26d64642d808de4a652579ef950d7cd2d37f080a8babf9c67c6b75fc4c3f/uv-0.13.9-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_WHEEL_SHA256="d7f459fe4fd247d830351c75686d3996ace3c6f3f728eb0e85a30b7ada0cef19"
PYTHON_VERSION="3.10"

DATA=1 BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --no-data) DATA=0 ;;
    --with-browser-test) BROWSER=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
  echo "This setup targets Linux x86_64 (WSL2 Ubuntu works). Found $(uname -s) $(uname -m)." >&2
  exit 2
fi

if [ ! -x .tools/bin/uv ] || ! .tools/bin/uv --version 2>/dev/null | grep -q "$UV_VERSION"; then
  echo "[setup] downloading uv $UV_VERSION (PyPI wheel, SHA-256 checked)"
  mkdir -p .tmp/dl .tools/bin
  curl -fsSL --retry 3 -o .tmp/dl/uv.whl "$UV_WHEEL_URL"
  echo "$UV_WHEEL_SHA256  .tmp/dl/uv.whl" | sha256sum -c -
  rm -rf .tmp/dl/uvwheel
  python3 -m zipfile -e .tmp/dl/uv.whl .tmp/dl/uvwheel 2>/dev/null || unzip -q -o .tmp/dl/uv.whl -d .tmp/dl/uvwheel
  cp .tmp/dl/uvwheel/uv-${UV_VERSION}.data/scripts/uv .tools/bin/uv
  chmod +x .tools/bin/uv
  rm -rf .tmp/dl/uvwheel .tmp/dl/uv.whl
fi
echo "[setup] $(.tools/bin/uv --version)"

echo "[setup] CPython $PYTHON_VERSION (managed by uv in .tools/python)"
uv python install "$PYTHON_VERSION"
if [ ! -x .venv/bin/python ]; then
  uv venv --python "$PYTHON_VERSION" .venv
fi
echo "[setup] installing hash-pinned dependencies (binary wheels only)"
uv pip sync --python .venv/bin/python --require-hashes --only-binary :all: requirements.lock

.venv/bin/python - <<'EOF'
import tenseal, numpy, tenseal.sealapi as s
from pmr.he.params import build_context, BFVParams
b = build_context(BFVParams())
print(f"[setup] TenSEAL {tenseal.__version__}, NumPy {numpy.__version__}; BFV N={b.params.poly_modulus_degree} "
      f"t={b.params.plain_modulus} q={[q.bit_length() for q in b.params.coeff_modulus]} slots={b.slot_count}: OK")
EOF

if [ "$DATA" = 1 ]; then
  for prof in smoke mini full; do
    if [ ! -f "data/catalog/$prof/manifest.json" ]; then
      echo "[setup] generating catalog $prof"
      .venv/bin/python -m pmr.audio.dataset generate --profile "$prof" --out "data/catalog/$prof"
    fi
    echo "[setup] verifying catalog $prof"
    .venv/bin/python -m pmr.audio.dataset verify "data/catalog/$prof" || {
      echo "[setup] WARNING: catalog $prof does not match its manifest (see README.md, Dataset and reproducibility)" >&2; }
  done
fi

if [ "$BROWSER" = 1 ]; then
  bash tests/browser/setup.sh
fi
echo "[setup] done. Next: scripts/test.sh, scripts/demo.sh"

#!/usr/bin/env bash
# Build dist/pmr-tcc-<UTC>.tar.gz for transfer to another machine (e.g. WSL2).
# Excludes local thesis/report material, environments, tools, caches, scratch files and build trees.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="pmr-tcc"
mkdir -p dist
out="dist/${name}-${stamp}.tar.gz"

EXCLUDES=(
  ./.git ./archives ./thesis ./docs ./legacy ./logs
  ./scripts/build_thesis.sh ./scripts/install_tectonic.sh ./scripts/capture_ui_figures.sh
  ./experiments/make_tex_tables.py ./tests/browser/capture_ui_figures.py ./bench/sealpir/fill_report_tables.py
  './.venv*' ./.tools ./.cache ./.home ./.config ./.local ./.tmp ./dist './third_party/archives/cmake-*'
  ./third_party/src ./third_party/build ./third_party/install
  ./data/catalog/generated ./results/pilot
)

# Safety: refuse to package anything that looks like secret key material outside excluded dirs.
suspicious=$(find . \( -path ./.tmp -o -path ./.venv -o -path ./.venv-browser -o -path ./.tools -o -path ./.cache \
  -o -path ./third_party -o -path ./dist -o -path ./.home -o -path ./archives \
  -o -path ./thesis -o -path ./docs -o -path ./legacy -o -path ./logs \) -prune -o -type f \
  \( -iname '*secret*key*' -o -iname '*.sk' -o -iname '*private*key*' -o -iname '*.pem' -o -iname 'id_rsa*' \) -print)
if [ -n "$suspicious" ]; then
  echo "refusing: files that look like key material:" >&2
  echo "$suspicious" >&2
  exit 1
fi

args=()
for e in "${EXCLUDES[@]}"; do args+=(--exclude="$e"); done
args+=(--exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='*.orig' --exclude='*.rej')

tar -czf "$out" --transform "s,^\.,${name}," "${args[@]}" .
(cd dist && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
tar -tzf "$out" | sort > "dist/${name}-${stamp}.MANIFEST.txt"
echo "package: $out ($(du -h "$out" | cut -f1)), $(wc -l < "dist/${name}-${stamp}.MANIFEST.txt") entries"
echo "sha256:  $(cat "$out.sha256")"

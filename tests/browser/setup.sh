#!/usr/bin/env bash
# One-time setup for the headless-browser playback test (everything stays inside the project):
#   1. separate virtualenv .venv-browser (CPython 3.10 from .tools/python), never the main .venv;
#   2. hash-pinned, binary-only install of tests/browser/requirements-browser.lock;
#   3. if Playwright's bundled Node.js cannot run here (glibc < 2.28), the same Node.js version
#      built for glibc 2.17 (unofficial-builds.nodejs.org), checked against a pinned SHA-256;
#   4. Chrome Headless Shell into .tools/ms-playwright (PLAYWRIGHT_BROWSERS_PATH from env.sh).
# Does NOT run `playwright install-deps` (that needs sudo/apt). Missing shared libraries are
# reported with ldd instead.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -L)"
cd "$ROOT"
# shellcheck source=/dev/null
source ./env.sh
# shellcheck source=browser_env.sh
source tests/browser/browser_env.sh

log() { printf '[browser-setup] %s\n' "$*"; }

# 1. virtualenv
if [ ! -x "$BROWSER_VENV/bin/python" ]; then
  log "creating $BROWSER_VENV"
  .tools/bin/uv venv --python "$BROWSER_PYTHON_VERSION" "$BROWSER_VENV"
fi

# 2. dependencies (hashes required, wheels only)
log "installing $BROWSER_LOCK"
.tools/bin/uv pip install --python "$BROWSER_VENV/bin/python" --require-hashes --only-binary :all: \
  -r "$BROWSER_LOCK"

# 3. Node.js for the Playwright driver
if "$(pmr_bundled_node)" --version >/dev/null 2>&1; then
  log "bundled Node.js runs on this host: $("$(pmr_bundled_node)" --version)"
else
  log "bundled Node.js cannot run here:"
  "$(pmr_bundled_node)" --version 2>&1 | sed 's/^/    /' || true
  if [ ! -x "$PW_NODE_DIR/bin/node" ]; then
    dl="$TMPDIR/browser/dl"
    mkdir -p "$dl"
    f="$dl/$(basename "$PW_NODE_URL")"
    log "downloading $PW_NODE_URL"
    curl -fsSL --max-time 600 -o "$f" "$PW_NODE_URL"
    echo "$PW_NODE_SHA256  $f" | sha256sum -c -
    tar -xJf "$f" -C "$ROOT/.tools"
  fi
  log "using $PW_NODE_DIR/bin/node $("$PW_NODE_DIR/bin/node" --version)"
fi
pmr_select_node || { log "no usable Node.js for the Playwright driver"; exit 2; }

# 4. browser
log "installing $PW_BROWSER into $PLAYWRIGHT_BROWSERS_PATH"
"$BROWSER_VENV/bin/python" -m playwright install "$PW_BROWSER"

# Report what the dynamic linker thinks (no system changes are made).
shell_bin="$(find "$PLAYWRIGHT_BROWSERS_PATH" -path '*chromium_headless_shell-*' -name chrome-headless-shell -type f | sort | tail -1)"
if [ -n "$shell_bin" ]; then
  log "headless shell: ${shell_bin#"$ROOT"/}"
  log "highest GLIBC symbol version required: $(objdump -T "$shell_bin" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -u -V | tail -1)"
  log "host glibc: $(getconf GNU_LIBC_VERSION 2>/dev/null || echo unknown)"
  missing="$(ldd "$shell_bin" 2>&1 | grep -i 'not found' || true)"
  if [ -n "$missing" ]; then
    log "MISSING shared libraries (cannot be installed without sudo):"
    printf '%s\n' "$missing" | sed 's/^/    /'
    exit 3
  fi
  log "ldd: no missing shared libraries"
fi
log "done"

# Shared settings for the headless-browser playback test.
# Sourced (after ./env.sh, which sets PMR_ROOT) by tests/browser/setup.sh and scripts/browser_test.sh.
# Everything lives inside the project: no system packages, no global installs.

# Separate virtualenv: the main .venv and requirements.lock are never touched.
BROWSER_VENV="$PMR_ROOT/.venv-browser"
BROWSER_PYTHON_VERSION=3.10
BROWSER_LOCK="$PMR_ROOT/tests/browser/requirements-browser.lock"

# Playwright's Python wheel bundles Node.js v26.13.1, whose official Linux build needs
# glibc >= 2.28. On older hosts (Amazon Linux 2: glibc 2.26) the driver is run with the same
# Node.js version from the Node.js project's "unofficial-builds" (built against glibc 2.17),
# selected through PLAYWRIGHT_NODEJS_PATH (supported by playwright-python's _driver.py).
PW_NODE_VERSION=v26.13.1
PW_NODE_FLAVOR=linux-x64-glibc-217
PW_NODE_SHA256=80c1972d71dd64ea6464c86f67182dce3c03d7d3de2be92c4f5bdc119d101497
PW_NODE_URL="https://unofficial-builds.nodejs.org/download/release/$PW_NODE_VERSION/node-$PW_NODE_VERSION-$PW_NODE_FLAVOR.tar.xz"
PW_NODE_DIR="$PMR_ROOT/.tools/node-$PW_NODE_VERSION-$PW_NODE_FLAVOR"

# Only the headless shell is needed (Chrome for Testing build, revision pinned by playwright).
PW_BROWSER=chromium-headless-shell

pmr_bundled_node() {
  echo "$BROWSER_VENV/lib/python$BROWSER_PYTHON_VERSION/site-packages/playwright/driver/node"
}

# Export PLAYWRIGHT_NODEJS_PATH when the bundled Node.js cannot run on this host.
# Returns non-zero if no usable Node.js is available.
pmr_select_node() {
  if [ -n "${PLAYWRIGHT_NODEJS_PATH:-}" ]; then
    "$PLAYWRIGHT_NODEJS_PATH" --version >/dev/null 2>&1
    return
  fi
  if "$(pmr_bundled_node)" --version >/dev/null 2>&1; then
    return 0
  fi
  if [ -x "$PW_NODE_DIR/bin/node" ] && "$PW_NODE_DIR/bin/node" --version >/dev/null 2>&1; then
    export PLAYWRIGHT_NODEJS_PATH="$PW_NODE_DIR/bin/node"
    return 0
  fi
  return 1
}

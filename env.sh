# Project-local environment. Source it from bash or zsh before running tools:
#     source ./env.sh
# Every cache, temporary file, tool, and environment stays inside the project.
# No machine-specific paths are hardcoded: PMR_ROOT is derived from this file.

if [ -n "${BASH_SOURCE:-}" ]; then
  _pmr_src="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  eval '_pmr_src="${(%):-%x}"'
else
  _pmr_src="$0"
fi
PMR_ROOT="$(cd "$(dirname "$_pmr_src")" && pwd -L)"
export PMR_ROOT
unset _pmr_src

# Isolated home and XDG directories so third-party tools never write to the real home.
export PMR_REAL_HOME="${PMR_REAL_HOME:-$HOME}"
export HOME="$PMR_ROOT/.home"
export XDG_CACHE_HOME="$PMR_ROOT/.cache"
export XDG_CONFIG_HOME="$PMR_ROOT/.config"
export XDG_DATA_HOME="$PMR_ROOT/.local/share"
export XDG_STATE_HOME="$PMR_ROOT/.local/state"
export TMPDIR="$PMR_ROOT/.tmp"
export TMP="$TMPDIR" TEMP="$TMPDIR"

# Python / pip: ignore global and user configuration, use the public PyPI index only.
export PYTHONNOUSERSITE=1
export PYTHONPATH="$PMR_ROOT/src"
export PYTHONPYCACHEPREFIX="$PMR_ROOT/.cache/pycache"
export PIP_CONFIG_FILE=/dev/null
export PIP_CACHE_DIR="$PMR_ROOT/.cache/pip"
export PIP_INDEX_URL="https://pypi.org/simple"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1

# uv (project-local binary in .tools/bin): no user config, managed Python inside the project.
export UV_NO_CONFIG=1
export UV_CACHE_DIR="$PMR_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PMR_ROOT/.tools/python"
export UV_PYTHON_BIN_DIR="$PMR_ROOT/.tools/bin"
export UV_TOOL_DIR="$PMR_ROOT/.tools/uv-tools"
export UV_TOOL_BIN_DIR="$PMR_ROOT/.tools/bin"
export UV_PYTHON_PREFERENCE=only-managed
export UV_DEFAULT_INDEX="https://pypi.org/simple"
export UV_NO_MODIFY_PATH=1
export UV_PROJECT_ENVIRONMENT="$PMR_ROOT/.venv"

# Other tools that may be used during the project.
export MPLCONFIGDIR="$PMR_ROOT/.cache/matplotlib"
export PLAYWRIGHT_BROWSERS_PATH="$PMR_ROOT/.tools/ms-playwright"
export CURL_HOME="$PMR_ROOT/.config"
export WGETRC=/dev/null
export DO_NOT_TRACK=1

# Bounded, reproducible CPU usage on a shared machine.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"

export PATH="$PMR_ROOT/.tools/nogit:$PMR_ROOT/.venv/bin:$PMR_ROOT/.tools/bin:$PATH"

mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" \
         "$TMPDIR" "$PMR_ROOT/.tools/bin"

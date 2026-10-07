#!/usr/bin/env bash
#
# run.sh - Entry point script for the Banking Bot.
#
# Usage:
#   ./run.sh                    # Start the bot
#   ./run.sh --once             # Run one cycle and exit
#   ./run.sh -- --help          # Pass args through to the bot
#   ./run.sh -r                 # Reinstall the virtualenv
#   ./run.sh -n                 # Skip dependency installation
#
# Environment variables:
#   PYTHON_BIN      Python interpreter to use (default: python3).
#   HEADLESS        Run browser in headless mode ("true" or "false").
#   CHECK_INTERVAL  Seconds between cycles (default: 300).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=11
MAX_PYTHON_MINOR=12
ENTRYPOINT="app.main"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    printf '[run.sh] %s\n' "$*"
}

error() {
    printf '[run.sh] ERROR: %s\n' "$*" >&2
}

usage() {
    cat <<EOF
Usage: ./run.sh [options] [-- bot-options]

Options:
  -h, --help      Show this help message and exit.
  -r, --reinstall Remove the virtual environment and reinstall dependencies.
  -n, --no-install Skip dependency installation (use existing venv as-is).
  --              Pass all remaining arguments to the bot (e.g. --once).

Environment variables:
  PYTHON_BIN      Python interpreter to use (default: python3).
  HEADLESS        Run browser in headless mode ("true" or "false").
  CHECK_INTERVAL  Seconds between cycles (default: 300).

Examples:
  ./run.sh --once                # one cycle, exit
  ./run.sh -- --help             # bot help
  ./run.sh -r --once             # reinstall, then one cycle

EOF
}

# ---------------------------------------------------------------------------
# Argument parsing (with passthrough to the bot)
# ---------------------------------------------------------------------------
REINSTALL=false
NO_INSTALL=false
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -r|--reinstall)
            REINSTALL=true
            shift
            ;;
        -n|--no-install)
            NO_INSTALL=true
            shift
            ;;
        --)
            shift
            PASSTHROUGH+=("$@")
            break
            ;;
        *)
            # Unknown flags are forwarded to the bot so that
            # `./run.sh --once` works without an explicit `--`.
            PASSTHROUGH+=("$1")
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
cd "${SCRIPT_DIR}"

if [[ ! -f "pyproject.toml" ]]; then
    error "pyproject.toml not found. Run this script from the project root."
    exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    error "'${PYTHON_BIN}' not found in PATH. Install Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ or set PYTHON_BIN."
    exit 1
fi

PYTHON_VERSION="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_MAJOR="${PYTHON_VERSION%%.*}"
PYTHON_MINOR="${PYTHON_VERSION##*.}"

if (( PYTHON_MAJOR < REQUIRED_PYTHON_MAJOR )) || \
   { (( PYTHON_MAJOR == REQUIRED_PYTHON_MAJOR )) && (( PYTHON_MINOR < REQUIRED_PYTHON_MINOR )); }; then
    error "Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ required, found ${PYTHON_VERSION}."
    exit 1
fi

if (( PYTHON_MAJOR == REQUIRED_PYTHON_MAJOR )) && (( PYTHON_MINOR > MAX_PYTHON_MINOR )); then
    error "Python 3.${MAX_PYTHON_MINOR} or lower required (Playwright has no wheels for 3.${PYTHON_MINOR} yet), found ${PYTHON_VERSION}."
    error "Set PYTHON_BIN to a supported interpreter (e.g. PYTHON_BIN=python3.12 ./run.sh)."
    exit 1
fi

log "Using Python ${PYTHON_VERSION} at $(command -v "${PYTHON_BIN}")"

# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------
if [[ "${REINSTALL}" == true && -d "${VENV_DIR}" ]]; then
    log "Removing existing virtual environment (--reinstall)."
    rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    log "Creating virtual environment at ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
log "Virtual environment activated."

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
if [[ "${NO_INSTALL}" == false ]]; then
    log "Upgrading pip..."
    python -m pip install --upgrade pip >/dev/null

    log "Installing project dependencies (editable)..."
    python -m pip install -e ".[dev]" >/dev/null

    log "Installing Playwright browser binaries..."
    python -m playwright install chromium
else
    log "Skipping dependency installation (--no-install)."
fi

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------
if [[ ! -f ".env" ]]; then
    error ".env file not found. Create one with BANKING_USERNAME and BANKING_PASSWORD."
    exit 1
fi

# ---------------------------------------------------------------------------
# Run the bot
# ---------------------------------------------------------------------------
log "Starting Banking Bot..."
if (( ${#PASSTHROUGH[@]} > 0 )); then
    log "Passing through: ${PASSTHROUGH[*]}"
fi
exec python -m "${ENTRYPOINT}" "${PASSTHROUGH[@]}"

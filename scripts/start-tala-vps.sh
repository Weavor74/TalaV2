#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ] || [ ! -f ".env" ]; then
    echo "First-run setup is incomplete. Running setup now..."
    uv run python scripts/first_run.py
fi

export WS_HOST="${WS_HOST:-0.0.0.0}"
export WS_PORT="${WS_PORT:-8765}"
export TALA_CHAIN_BIND_HOST="${TALA_CHAIN_BIND_HOST:-0.0.0.0}"
export TALA_CHAIN_BIND_PORT="${TALA_CHAIN_BIND_PORT:-8332}"
export INFERENCE_MODE="${INFERENCE_MODE:-local}"
export TALA_VPS_ACCEPT_ANY_TALA="${TALA_VPS_ACCEPT_ANY_TALA:-1}"

echo "Starting Tala VPS blockchain discovery node on http://${TALA_CHAIN_BIND_HOST}:${TALA_CHAIN_BIND_PORT}..."
TALA_CHAIN_STANDALONE=1 node blockchain/src/app.js &
CHAIN_PID=$!

cleanup() {
    if kill -0 "$CHAIN_PID" 2>/dev/null; then
        kill "$CHAIN_PID"
    fi
}
trap cleanup EXIT INT TERM

echo "Starting Tala VPS WebSocket harness on ${WS_HOST}:${WS_PORT}..."
"$VENV_PY" -m python.harness.server

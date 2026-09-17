#!/usr/bin/env bash
set -e

echo "========================================"
echo " Starting Tala Cognitive Engine Room"
echo "========================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ] || [ ! -f "$ROOT_DIR/.env" ]; then
    echo "First-run setup is incomplete. Running setup now..."
    uv run python scripts/first_run.py
fi

# Electron owns the harness lifecycle; do not race it with a second backend.
echo "Starting Tala Electron UI..."
exec npm start

# File: `start-tala.sh`

## Overview

The primary launch script for starting both the Python harness and Electron UI concurrently.

## File Contents

```bash
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

echo "Starting Tala Python Backend..."
"$VENV_PY" -m python.harness.server &
BACKEND_PID=$!

cleanup() {
    echo "UI closed or interrupted. Shutting down backend..."
    if kill -0 $BACKEND_PID 2>/dev/null; then
        kill $BACKEND_PID
    fi
}
trap cleanup EXIT INT TERM

echo "Starting Tala Electron UI..."
npm start

```

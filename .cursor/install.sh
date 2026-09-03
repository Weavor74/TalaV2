#!/usr/bin/env bash
#
# Cloud Agent install phase for the Tala Cognitive Engine Room.
#
# Runs once after checkout to build the baseline environment snapshot. Must be
# idempotent: it may run again over a warm checkout or cached state. Per-boot
# services (PostgreSQL, blockchain node, harness/web UI) are NOT started here —
# they live in .cursor/start.sh and the environment.json terminals.
#
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
# Pin the interpreter uv uses everywhere below to match CI (Python 3.11).
export UV_PYTHON=3.11

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "== [1/6] System packages (PostgreSQL) =="
if ! command -v psql >/dev/null 2>&1; then
  sudo apt-get update -yqq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yqq postgresql postgresql-contrib
else
  echo "  - PostgreSQL already present: $(psql --version)"
fi

echo "== [2/6] uv (Python package/toolchain manager) =="
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "  - uv already present: $(uv --version)"
fi
uv python install 3.11

echo "== [3/6] Start PostgreSQL (install-time only, to provision roles/dbs) =="
sudo pg_ctlcluster 16 main start || true
for i in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done
sudo -u postgres pg_isready

echo "== [4/6] Provision databases (app 'tala' + sibling test db 'tala_test') =="
# The app database + role. bootstrap-db.sh must run as root so it drives psql
# through the local 'postgres' superuser via peer auth.
sudo bash scripts/bootstrap-db.sh tala tala tala
# conftest.py redirects the pytest suite to a sibling '<db>_test' database so it
# never mutates the app's data. Provision it here so the test suite can connect.
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='tala_test'" | grep -q 1; then
  sudo -u postgres psql -c 'CREATE DATABASE "tala_test" OWNER "tala";'
  echo "  - created database tala_test"
else
  echo "  - database tala_test already exists"
fi

echo "== [5/6] Project dependencies + schema (scripts/first_run.py) =="
# Creates .env with local defaults, runs `npm ci`, `uv sync` (core + test +
# voice + transformers extras), and initialises the PostgreSQL schema. Idempotent.
uv run --python 3.11 python scripts/first_run.py

echo "== [6/6] Pre-warm embedded inference model (non-fatal) =="
# The harness lazily downloads Qwen/Qwen2.5-0.5B-Instruct (~1GB) for local
# inference on first start. Caching it now bakes it into the snapshot so fresh
# agents start instantly and work offline. Failure here is non-fatal.
uv run --python 3.11 python - <<'PY' || echo "  - model pre-warm skipped (will download at first harness start)"
from huggingface_hub import snapshot_download
snapshot_download("Qwen/Qwen2.5-0.5B-Instruct")
print("  - embedded model cached")
PY

echo "== Tala install complete =="

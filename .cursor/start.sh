#!/usr/bin/env bash
#
# Cloud Agent start phase for the Tala Cognitive Engine Room.
#
# Runs on every boot before the terminals launch. Its only job is per-boot
# runtime reconciliation: bring PostgreSQL up (its data directory, roles and
# databases were provisioned during install and captured in the snapshot) and
# wait until it is ready. It must be idempotent and must return.
#
set -euo pipefail

echo "== Starting PostgreSQL =="
# Exit code 2 means "already running", which is fine here.
sudo pg_ctlcluster 16 main start || true

for i in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    echo "  - PostgreSQL is accepting connections"
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL did not become ready in time" >&2
exit 1

"""
scripts/promote_staged_nodes.py — run one node-promotion cycle
==============================================================

New nodes Tala authors land in ``nodes/_staging/`` on probation. Each run
of this script verifies every staged node once; a node that passes
``TALA_NODE_PROMOTION_PASSES`` consecutive cycles (default 3) is moved
into ``nodes/``, and one that fails repeatedly is moved to
``nodes/_quarantine/`` with the reason recorded beside it.

The harness runs a cycle at startup, so in normal operation this script is
for looking at the queue and for forcing the issue by hand.

Usage
-----
    python scripts/promote_staged_nodes.py             # run a cycle
    python scripts/promote_staged_nodes.py --list      # show queue only
    python scripts/promote_staged_nodes.py --dry-run   # verify, promote nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sdk.agent.node_staging import (  # noqa: E402
    list_staged,
    promotion_passes,
    run_verification_cycle,
)


def _print_queue() -> None:
    staged = list_staged()
    if not staged:
        print("staging queue is empty")
        return
    print(f"{len(staged)} node(s) on probation (need {promotion_passes()} passing cycles):\n")
    for row in staged:
        bar = f"{row['passes']}/{row['required_passes']}"
        line = f"  {row['node_id']:<58} {bar}"
        if row.get("last_error"):
            line += f"   last error: {row['last_error'][:70]}"
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show the queue and exit")
    parser.add_argument("--dry-run", action="store_true", help="verify without promoting")
    args = parser.parse_args()

    if args.list:
        _print_queue()
        return 0

    summary = run_verification_cycle(promote=not args.dry_run)

    for node_id in summary["promoted"]:
        print(f"promoted:    {node_id}")
    for item in summary["quarantined"]:
        print(f"quarantined: {item['node_id']} — {item['reason']}")
    for item in summary["pending"]:
        state = f"{item['passes']}/{item['required']}"
        suffix = f" — {item['last_error']}" if item.get("last_error") else ""
        print(f"pending:     {item['node_id']} ({state}){suffix}")

    if not any((summary["promoted"], summary["quarantined"], summary["pending"])):
        print("staging queue is empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

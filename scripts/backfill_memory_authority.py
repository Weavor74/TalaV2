#!/usr/bin/env python
"""
scripts/backfill_memory_authority.py

Grant recall authority to nodes that were generated unable to be remembered.

THE DEFECT
----------
Node activation is a pure product:

    activation = semantic_match * relevance * confidence * freshness
                 * provider_trust * governance

and the engine's own docstring says governance "acts as a hard gate: setting it
to 0.0 blocks activation entirely". The retrieval path uses
max(execution_authority, memory_authority) as that gate.

Documentation nodes are correctly written with execution_authority 0.00 -- a
document must never be able to execute anything. The same 0.00 was then copied
into memory_authority, which does not mean "cannot act", it means "cannot be
recalled". Applied to nodes whose entire purpose is recall.

Result, measured on 115 nodes: 94 could never be injected into a prompt at any
similarity. 92 of those are node_type="documentation" -- the AST-derived
sdk_agent_* and program-doc-* set, plus the learning nodes. Retrieval still
answered every query, using the ~18% of the graph that had authority, which is
mostly settings. Asking "how do I write an HTML neural node" returned
settings-code-registry at 0.26 similarity while system-html-nn-authoring-guide,
at 0.43, scored exactly zero.

THE VALUE
---------
0.60: recallable, but below the live settings and doctrine nodes at 1.00, so a
strongly-matching document outranks a weakly-matching setting without letting
documentation drown out current configuration. In-repo precedent: corpus story
nodes 0.40, harness-authored nodes 0.80, settings 1.00.

execution_authority is deliberately NOT touched. Documentation must stay unable
to execute; that zero was always right.

USAGE
    python scripts/backfill_memory_authority.py --dry-run
    python scripts/backfill_memory_authority.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Node types whose reason for existing is being recalled. Anything else keeps
# whatever it declares -- this script grants recall, it does not normalise
# authority across the graph.
RECALLABLE_TYPES = {"documentation", "function-doc", "specification", "learning"}

DEFAULT_MEMORY_AUTHORITY = "0.60"

_MEM_RE = re.compile(r'(data-memory-authority=")([^"]*)(")')
_TYPE_RE = re.compile(r'data-node-type="([^"]*)"')
_EXEC_RE = re.compile(r'data-execution-authority="([^"]*)"')


def _as_float(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def scan(nodes_dir: Path) -> list[tuple[Path, str, str]]:
    """Return (path, node_type, current_memory_authority) for each candidate."""
    out = []
    for path in sorted(nodes_dir.glob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        type_match = _TYPE_RE.search(text)
        mem_match = _MEM_RE.search(text)
        if not type_match or not mem_match:
            continue
        node_type = type_match.group(1)
        current = mem_match.group(2)
        if node_type not in RECALLABLE_TYPES:
            continue
        if _as_float(current) > 0.0:
            continue
        # Only nodes that are gated to zero overall. A node with execution
        # authority is already recallable through max(); leave it alone.
        exec_match = _EXEC_RE.search(text)
        if exec_match and _as_float(exec_match.group(1)) > 0.0:
            continue
        out.append((path, node_type, current))
    return out


def apply(candidates: list[tuple[Path, str, str]], value: str) -> int:
    changed = 0
    for path, _node_type, _current in candidates:
        text = path.read_text(encoding="utf-8")
        new_text, count = _MEM_RE.subn(rf'\g<1>{value}\g<3>', text, count=1)
        if count and new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes-dir", default=str(REPO_ROOT / "nodes"))
    ap.add_argument("--value", default=DEFAULT_MEMORY_AUTHORITY)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    nodes_dir = Path(args.nodes_dir)
    if not nodes_dir.is_dir():
        print(f"Not a directory: {nodes_dir}")
        return 2

    candidates = scan(nodes_dir)
    total = len(list(nodes_dir.glob("*.html")))
    print(f"{nodes_dir}: {total} nodes, {len(candidates)} unrecallable and eligible\n")

    by_type: dict[str, int] = {}
    for _path, node_type, _current in candidates:
        by_type[node_type] = by_type.get(node_type, 0) + 1
    for node_type, count in sorted(by_type.items()):
        print(f"  {node_type:<20} {count:>4}")

    if args.dry_run:
        print(f"\nDry run. Would set data-memory-authority=\"{args.value}\" on {len(candidates)} nodes.")
        for path, _t, _c in candidates[:10]:
            print(f"  {path.name}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")
        return 0

    changed = apply(candidates, args.value)
    print(f"\nSet data-memory-authority=\"{args.value}\" on {changed} nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

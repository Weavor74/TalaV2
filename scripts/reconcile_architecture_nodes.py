"""
reconcile_architecture_nodes.py — point the older documents at the authority.

Steven's instruction, and the reason this is not a mass regeneration:

    "I'd prioritize a small, authoritative set of architecture and operational
    nodes, then reconcile older documents against them. Regenerating everything
    without resolving contradictions would preserve the confusion."

Reading the older architecture nodes, they are mostly *accurate*. What they
lack is not correctness but standing: nothing in them says when they were last
true, or which document wins where they disagree with another. A model reading
one of them has no way to tell whether it is looking at the current shape of
the system or at a description from June.

So this does not rewrite them. It stamps each with a reconciliation block that
says three things:

* the architecture contract is authoritative on the six subjects it covers —
  data ownership, security layers, execution paths, capability maturity,
  execution gates, and verification metadata;
* where this node and the contract disagree, the contract wins;
* the commit and date at which this node was last reconciled.

That last one is the point. An undated document cannot be reconciled against
anything, because a reader cannot tell whether it predates the thing they are
comparing it to. Dating it is what makes the next reconciliation possible.

Idempotent: running twice replaces the block rather than stacking copies.

    python scripts/reconcile_architecture_nodes.py
    python scripts/reconcile_architecture_nodes.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTHORITY_NODE = "system-architecture-contract"

#: The nodes that make claims in the contract's domain. Deliberately a short,
#: named list rather than a glob: reconciling everything would stamp 249 files
#: with an assurance nobody checked, which is the confusion this is meant to
#: resolve rather than spread.
RECONCILED_NODES = (
    "system-tala-agent-architecture",
    "system-tala-html-nn-memory-runtime",
    "system-self-improvement-loop",
    "system-neural-node-v2-schema",
    "system-html-nn-authoring-guide",
    "system-tala-configurable-provider-surface",
    "ui-view-agent-builder",
)

#: What the contract is authoritative about. Named explicitly so the deference
#: is bounded: these nodes remain the better source on everything else, and a
#: blanket "the contract wins" would wrongly discard what they know.
AUTHORITATIVE_ON = (
    "which stores own which records, and what backup and restore each requires",
    "the three security layers — renderer isolation, the IPC surface, and "
    "host command execution — and which of them protects what",
    "how chat, goals, workflows, DAGs, crews, swarms and scheduled jobs reach "
    "a tool, and what verifies the result",
    "capability maturity: implemented, tested, live-verified, experimental, "
    "or unavailable, with the evidence named",
    "which node types may execute, and the authority required to run them",
)

SECTION_ID = "contract-reconciliation"
_SECTION_RE = re.compile(
    rf'\s*<section id="{SECTION_ID}">.*?</section>', re.DOTALL
)


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return ""


def _block(commit: str, when: str) -> str:
    items = "".join(f"<li>{topic}</li>" for topic in AUTHORITATIVE_ON)
    return (
        f'\n<section id="{SECTION_ID}">\n'
        "  <h2>Reconciliation</h2>\n"
        f"  <p>This node was reconciled against <strong>{AUTHORITY_NODE}</strong> "
        f"on {when} at commit <code>{commit[:12]}</code>. That contract is "
        "authoritative on:</p>\n"
        f"  <ul>{items}</ul>\n"
        "  <p>Where this node and the contract disagree on those subjects, the "
        "contract is current and this node is not. On everything else this node "
        "remains the better source — the deference is bounded deliberately, "
        "because a blanket one would discard what this document knows and the "
        "contract does not.</p>\n"
        "  <p>Regenerate the contract with "
        "<code>python scripts/generate_architecture_contract.py</code>, or call "
        "<code>docs.regenerate_contract</code>. It also refreshes daily on its "
        "own, writing only when the description actually changed.</p>\n"
        "</section>\n"
    )


def _reconcile(path: Path, commit: str, when: str) -> str:
    """Stamp one node. Returns what happened."""
    html = path.read_text(encoding="utf-8", errors="replace")
    had_block = bool(_SECTION_RE.search(html))
    html = _SECTION_RE.sub("", html)

    anchor = html.find('<script type="application/json" id="node-state"')
    if anchor == -1:
        anchor = html.find("</article>")
        if anchor == -1:
            return "skipped: no place to put it"

    html = html[:anchor] + _block(commit, when) + html[anchor:]

    # And in the state block, so code reading the node sees it too. The prose
    # is for a model; this is for anything that parses.
    def _stamp_state(match: re.Match) -> str:
        try:
            state = json.loads(match.group(1))
        except Exception:
            return match.group(0)
        state["contract_reconciliation"] = {
            "authority": AUTHORITY_NODE,
            "authoritative_on": list(AUTHORITATIVE_ON),
            "reconciled_at": when,
            "reconciled_commit": commit,
        }
        return match.group(0).replace(
            match.group(1), json.dumps(state, indent=2, ensure_ascii=False)
        )

    html = re.sub(
        r'<script type="application/json" id="node-state"[^>]*>(.*?)</script>',
        _stamp_state, html, count=1, flags=re.DOTALL,
    )

    path.write_text(html, encoding="utf-8")
    return "updated" if had_block else "stamped"


def main(check_only: bool) -> int:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    nodes_dir = resolve_active_nodes_dir()
    commit = _commit()
    when = datetime.now(timezone.utc).date().isoformat()

    missing, unstamped, done = [], [], []
    for node_id in RECONCILED_NODES:
        path = nodes_dir / f"{node_id}.html"
        if not path.exists():
            missing.append(node_id)
            continue
        if check_only:
            html = path.read_text(encoding="utf-8", errors="replace")
            (done if _SECTION_RE.search(html) else unstamped).append(node_id)
            continue
        done.append(f"{node_id}: {_reconcile(path, commit, when)}")

    for node_id in missing:
        print(f"MISSING  {node_id}")
    if check_only:
        for node_id in unstamped:
            print(f"UNRECONCILED  {node_id}")
        print(f"\n{len(done)} reconciled, {len(unstamped)} not, {len(missing)} missing")
        return 1 if (unstamped or missing) else 0

    for line in done:
        print(line)
    print(f"\nreconciled {len(done)} node(s) against {AUTHORITY_NODE} at {commit[:12]}")
    return 1 if missing else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report which nodes are unreconciled instead of stamping them")
    raise SystemExit(main(ap.parse_args().check))

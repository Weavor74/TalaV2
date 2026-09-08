"""
doc_maintenance.py — keep the self-description current without being asked.

Steven: *"regen should commence at least everyday."*

Tala reads her own documentation as context when reasoning about herself. She
was once described as self-healing — documentation updated whenever code or
documents changed — and that is not what happens now: there are no git hooks,
the heartbeat table holds no such job, and regeneration runs only when someone
asks in chat, calls the RPC, or completes an autonomous goal. So the contract
was months behind the code, confidently.

This is the daily pass. Two decisions in it are worth stating.

**It is deterministic, not a goal.** The heartbeat scheduler fires
``goal_text`` through the model, which for a task with one correct answer
means paying for inference to maybe get it. Regenerating a contract that is
computed from the code is a script, so it runs as a script.

**It writes only when the substance changed.** Every generation stamps a fresh
timestamp and the current commit, so writing unconditionally would rewrite the
node every day, anchor a transaction on the chain every day, and leave a year
of diffs saying nothing happened. Noise costs the same attention as signal and
teaches you to ignore the file. A no-change pass is logged and nothing else.

That makes the stamp mean something precise: **when the description last
changed**, not when a scheduler last ran.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How often the pass runs. Daily is what was asked for; it is also about the
#: rate at which this system's architecture actually moves.
INTERVAL_SECONDS = 24 * 60 * 60

#: How long after startup the first pass runs. Long enough to be behind the
#: database, providers and node loading, which all matter more than this.
FIRST_RUN_DELAY_SECONDS = 300

CONTRACT_NODE_ID = "system-architecture-contract"


def _published_contract(node_path: Path) -> dict[str, Any] | None:
    """The contract as it currently stands on disk, or None."""
    if not node_path.exists():
        return None
    try:
        html = node_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for block in re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if isinstance(data, dict):
            if "architecture_contract" in data:
                return data["architecture_contract"]
            inner = data.get("state_data")
            if isinstance(inner, dict) and "architecture_contract" in inner:
                return inner["architecture_contract"]
    return None


#: Fields that move without the system moving. Excluded from the comparison
#: that decides whether to rewrite the node.
#:
#: Two kinds. The verification stamp carries a timestamp and a commit, which
#: change on every generation. The rest are a CENSUS -- how many nodes, how
#: many rows indexed -- and those drift constantly: every session indexed,
#: every node written, including this node writing itself. Counting them as
#: change would rewrite the contract every day whether or not it now said
#: anything different, which is the churn this pass exists to avoid.
#:
#: Structure is compared and census is not: a store appearing, a capability
#: changing maturity, an executable node type being added are all still seen.
VOLATILE_PATHS = (
    ("verification",),
    ("stores", "html_nn", "count"),
    ("stores", "html_nn", "families"),
    ("stores", "chroma_node_index", "indexed"),
    ("stores", "postgres_memory_index", "by_source_type"),
)


def _strip(data: Any, path: tuple[str, ...]) -> None:
    """Remove one dotted path from a nested dict, if it is there."""
    if not path:
        return
    node = data
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    if isinstance(node, dict):
        node.pop(path[-1], None)


def _substance(contract: dict[str, Any] | None) -> str:
    """
    What the contract SAYS, with what it merely counted removed.

    The comparison that decides whether to write. See VOLATILE_PATHS for what
    is left out and why.
    """
    if not contract:
        return ""
    trimmed = json.loads(json.dumps(contract, default=str))
    for path in VOLATILE_PATHS:
        _strip(trimmed, path)
    return json.dumps(trimmed, sort_keys=True, ensure_ascii=False, default=str)


async def regenerate_contract_if_changed() -> dict[str, Any]:
    """
    Rebuild the architecture contract, and write it only if it now says
    something different.

    Returns what it decided and why, so a caller — or a person reading the
    log — can tell "checked, unchanged" from "never ran", which are the two
    states a silent maintenance task is otherwise indistinguishable between.
    """
    try:
        from sdk.agent.documentation_nodes import (
            create_documentation_node,
            resolve_active_nodes_dir,
        )
        # The generator owns the shape of the contract; this owns when it runs.
        import sys

        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "scripts"))
        from generate_architecture_contract import _prose, build_contract
    except Exception as exc:
        log.warning("doc maintenance: could not load the contract generator: %s", exc)
        return {"ran": False, "reason": str(exc)}

    try:
        contract = await build_contract()
    except Exception as exc:
        log.exception("doc maintenance: contract generation failed")
        return {"ran": False, "reason": str(exc)}

    node_path = resolve_active_nodes_dir() / f"{CONTRACT_NODE_ID}.html"
    before = _substance(_published_contract(node_path))
    after = _substance(contract)

    if before and before == after:
        log.info(
            "doc maintenance: architecture contract checked at commit %s — unchanged",
            (contract.get("verification") or {}).get("reviewed_commit", "?")[:12],
        )
        return {"ran": True, "changed": False, "wrote": False}

    try:
        result = create_documentation_node(
            node_id=CONTRACT_NODE_ID,
            title="Architecture Contract",
            doc_target="tala.architecture",
            node_type="documentation",
            semantic_memory=_prose(contract),
            tags=["architecture", "contract", "ontology", "system",
                  "defaults", "orientation", "read-first"],
            state_extra={"architecture_contract": contract},
        )
    except Exception as exc:
        log.exception("doc maintenance: could not write the contract node")
        return {"ran": True, "changed": True, "wrote": False, "reason": str(exc)}

    log.info(
        "doc maintenance: architecture contract %s — %s",
        "created" if not before else "updated because the system changed",
        result.get("path") or result,
    )
    return {"ran": True, "changed": True, "wrote": True, "path": str(result.get("path") or "")}


async def run_forever() -> None:
    """
    The daily pass, for the lifetime of the harness.

    Never raises. A maintenance loop that can take down the process it
    maintains is worse than one that occasionally misses a day, and its
    failures are already reported through the return value above.
    """
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            await regenerate_contract_if_changed()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("doc maintenance: pass failed; continuing")
        await asyncio.sleep(INTERVAL_SECONDS)

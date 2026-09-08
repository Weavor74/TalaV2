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

**It covers two kinds of staleness.** The contract goes stale when the shape
of the system changes; a module's documentation goes stale when somebody edits
the module. The second is what Steven asked for -- *"if a bit of code gets
updated ... the documents need to be rewritten for that"* -- and it is checked
by hashing the source each document names and comparing it against what was
hashed when the document was written.

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

#: Defined where every regeneration path can see it, so a rewrite cannot
#: quietly clear a warning that is still true.
from sdk.agent.documentation_nodes import UNVERIFIED_MARK, UNVERIFIED_NOTICE


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
#: change on every generation. The other two are indexes that move on their
#: own: sessions are indexed continuously and Chroma re-indexes lazily, so
#: counting either as a change would rewrite the contract every day whether
#: or not it now said anything different.
#:
#: The NODE count is deliberately not in this list, though it is a census
#: too. It moves only when nodes are added or removed, which is a real change
#: to what the contract asserts -- and `test_it_is_measured_not_asserted`
#: fails when the published count is wrong. Excluding it made the daily pass
#: unable to fix the one number a separate ratchet already demanded be right.
VOLATILE_PATHS = (
    ("verification",),
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


async def refresh_stale_documentation() -> dict[str, Any]:
    """
    Rewrite the documents whose code has moved on since they were written.

    Steven's requirement, in his words: *"if a bit of code gets updated, lets
    say from using a function on amazon to using an API key or something
    similar the documents need to be rewritten for that."*

    Tala reads these nodes to understand her own code before modifying it or
    explaining it to somebody. A document that is confidently out of date is
    worse than a missing one: a gap sends you to the source, and a wrong
    answer does not.

    Regeneration is deterministic — the prose is derived from the module's
    own docstrings and its public classes and functions, read straight out of
    the AST. No model is called, so this cannot invent an explanation for
    code it has misread, and it costs nothing to run on every module that
    changed.

    What it cannot regenerate it still reports. A node whose source it does
    not know how to rebuild is named rather than quietly left alone, because
    the point is that nothing describing changed code stays unexamined.
    """
    try:
        from sdk.agent.documentation_nodes import documentation_staleness
    except Exception as exc:
        log.warning("doc maintenance: staleness check unavailable: %s", exc)
        return {"ran": False, "reason": str(exc)}

    try:
        report = documentation_staleness()
    except Exception as exc:
        log.exception("doc maintenance: staleness check failed")
        return {"ran": False, "reason": str(exc)}

    stale = report.get("stale") or []
    if not stale:
        log.info(
            "doc maintenance: %d document(s) checked against their source — all current",
            report.get("current", 0),
        )
        return {"ran": True, "stale": 0, "rewritten": 0,
                "current": report.get("current", 0),
                "unknown": len(report.get("unknown") or [])}

    rewritten, unhandled, unverified = [], [], []
    for entry in stale:
        target = entry.get("doc_target") or ""
        before = _semantic_memory_of(Path(entry["path"])) if entry.get("path") else ""
        if not _regenerate_module_node(target):
            unhandled.append({"node_id": entry["node_id"], "doc_target": target})
            continue

        rewritten.append(entry["node_id"])

        # The distinction that matters, and the one a digest alone hides.
        #
        # The digest covers the whole file; the derived prose covers the
        # module docstring and its public names and their docstrings. Change
        # a function BODY and leave its docstring alone -- swap an Amazon
        # call for an API key, say -- and the digest moves, this rewrites the
        # node, and the description comes out byte-identical, still
        # describing Amazon. The node is then honestly source-current and
        # quietly wrong, which is worse than being marked stale.
        after = _semantic_memory_of(Path(entry["path"])) if entry.get("path") else ""
        if before and after and before == after:
            unverified.append({
                "node_id": entry["node_id"],
                "doc_target": target,
                "why": "the source changed inside code this description does not cover",
            })
            _mark_description_unverified(Path(entry["path"]))

    log.info(
        "doc maintenance: %d document(s) had changed code — rewrote %d, %d came "
        "out unchanged and need a human or a model to look (%s), %d have no "
        "generator (%s)",
        len(stale), len(rewritten), len(unverified),
        ", ".join(u["node_id"] for u in unverified) or "none",
        len(unhandled), ", ".join(u["node_id"] for u in unhandled) or "none",
    )
    return {"ran": True, "stale": len(stale), "rewritten": len(rewritten),
            "rewritten_nodes": rewritten, "unhandled": unhandled,
            "description_unverified": unverified,
            "current": report.get("current", 0),
            "unknown": len(report.get("unknown") or [])}


def _semantic_memory_of(path: Path) -> str:
    """The recalled text of a node, for comparing one generation to the next."""
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'<section id="semantic-memory">(.*?)</section>', html, re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


def _mark_description_unverified(path: Path) -> None:
    """
    Say so on the node, and say it where recall will see it.

    Recording this only in an attribute would repeat the mistake the
    reconciliation block made: perfectly stored, and invisible to the path
    that actually feeds a model. A document describing behaviour that has
    since changed must announce that in its own text.
    """
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    if UNVERIFIED_MARK in html:
        return

    html = re.sub(
        r'(<section id="semantic-memory">)',
        lambda m: m.group(1) + "\n" + UNVERIFIED_NOTICE, html, count=1,
    )
    if 'data-description-verified=' in html:
        html = re.sub(r'data-description-verified="[^"]*"',
                      'data-description-verified="false"', html, count=1)
    else:
        html = re.sub(r'(data-source-digest="[^"]*")',
                      r'\1\n  data-description-verified="false"', html, count=1)
    try:
        path.write_text(html, encoding="utf-8")
    except OSError:
        log.warning("could not mark %s as unverified", path.stem)


def _regenerate_module_node(doc_target: str) -> bool:
    """
    Rebuild one module's documentation node from its source. True if done.

    Only module documentation is handled here. A doc_target naming something
    else — a UI view, a program area, a persona — has no derivation this
    function knows, and saying so is more useful than pretending.
    """
    import sys

    root = Path(__file__).resolve().parents[2]
    for candidate in (str(root), str(root / "scripts")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)

    try:
        from generate_sdk_agent_nodes import (
            DOCUMENTED_PACKAGES, _describe_module, _semantic_memory,
        )
        from sdk.agent.documentation_nodes import create_documentation_node
    except Exception as exc:
        log.warning("doc maintenance: module documenter unavailable: %s", exc)
        return False

    for spec in DOCUMENTED_PACKAGES:
        prefix = spec["module_prefix"] + "."
        if not doc_target.startswith(prefix):
            continue
        stem = doc_target[len(prefix):]
        rel_dir = "/".join(spec["directory"])
        path = root.joinpath(*spec["directory"]) / f"{stem}.py"
        if not path.exists():
            return False
        info = _describe_module(path)
        if info is None:
            return False
        try:
            create_documentation_node(
                node_id=f"{spec['node_prefix']}_{stem}",
                title=f"{spec['title_prefix']}: {stem.replace('_', ' ').title()}",
                doc_target=doc_target,
                semantic_memory=_semantic_memory(doc_target, f"{rel_dir}/{path.name}", info),
                source_files=[f"{rel_dir}/{path.name}"],
                tags=["html-nn", "documentation", *spec["tags"], stem],
            )
            return True
        except Exception:
            log.exception("doc maintenance: could not rewrite %s", doc_target)
            return False
    return False


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
            # And every document whose code has moved since it was written.
            # The contract describes the shape of the system; these describe
            # the modules, and they go stale for a different reason -- somebody
            # edited the code they explain.
            await refresh_stale_documentation()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("doc maintenance: pass failed; continuing")
        await asyncio.sleep(INTERVAL_SECONDS)

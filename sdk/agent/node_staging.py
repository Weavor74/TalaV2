"""
sdk/agent/node_staging.py — probation for newly authored neural nodes
=====================================================================

A node Tala writes about something she just learned is a guess until it
has survived contact with the system. This module gives those nodes a
probation period: they land in ``nodes/_staging/``, are re-verified on
every cycle, and are promoted into ``nodes/`` only after passing a
configurable number of consecutive cycles. Repeated failures send them to
``nodes/_quarantine/`` instead of the wiki.

Two problems this addresses, both real:

* **Junk in the wiki.** Auto-authored learning nodes were minted per
  completed goal and only 3 of 17 were worth keeping. A quality gate at
  write time catches the obvious ones; probation catches the rest, because
  a node that cannot be parsed, has no real content, or whose embedded
  code will not compile fails every cycle and never reaches production.
* **Unverified writes going straight into the one copy that exists.**
  Production nodes are the retrieval corpus. Anything that lands there is
  immediately visible to context injection, so a malformed node degrades
  every answer until someone notices.

``nodes/_staging`` is deliberately a *subdirectory* of the nodes
directory: every consumer (retrieval indexing, the node grid, the wiki
healer) globs ``nodes/*.html`` non-recursively, so staged nodes are
invisible to all of them without a single change at those call sites.

Configuration
-------------
``TALA_NODE_PROMOTION_PASSES``   consecutive passes required (default 3)
``TALA_NODE_STAGING``            set to "off" to write straight through
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STAGING_DIRNAME = "_staging"
QUARANTINE_DIRNAME = "_quarantine"
LEDGER_FILENAME = ".promotion_ledger.json"

DEFAULT_PROMOTION_PASSES = 3
DEFAULT_MAX_FAILURES = 3

#: Below this, a node's semantic memory is a stub rather than content.
MIN_SEMANTIC_MEMORY_CHARS = 80

_PLACEHOLDER_MARKERS = ("auto-scaffolded", "placeholder, not authored")
_SEMANTIC_RE = re.compile(r'<section[^>]*id="semantic-memory"[^>]*>(.*?)</section>', re.S | re.I)
_PY_BLOCK_RE = re.compile(
    r'<script[^>]*type="application/x-python"[^>]*>(.*?)</script>', re.S | re.I
)


def staging_enabled() -> bool:
    return str(os.environ.get("TALA_NODE_STAGING", "")).strip().lower() not in ("off", "0", "false")


def promotion_passes() -> int:
    raw = os.environ.get("TALA_NODE_PROMOTION_PASSES")
    try:
        return max(1, int(raw)) if raw else DEFAULT_PROMOTION_PASSES
    except ValueError:
        return DEFAULT_PROMOTION_PASSES


def _nodes_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    return resolve_active_nodes_dir()


def resolve_staging_dir() -> Path:
    d = _nodes_dir() / STAGING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_quarantine_dir() -> Path:
    d = _nodes_dir() / QUARANTINE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    return resolve_staging_dir() / LEDGER_FILENAME


def _load_ledger() -> dict[str, Any]:
    try:
        p = _ledger_path()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("node_staging: unreadable ledger (%s) — starting fresh", exc)
    return {}


def _save_ledger(ledger: dict[str, Any]) -> None:
    try:
        _ledger_path().write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("node_staging: could not write ledger: %s", exc)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_node_file(path: Path) -> tuple[bool, str | None]:
    """
    Check that a staged node is a real, usable neural node.

    Returns ``(ok, reason)``. Every check is something that would actively
    harm the wiki if it reached production: an unparseable node breaks
    retrieval indexing, a stub pollutes context injection with nothing, and
    embedded code that will not compile cannot be executed as a skill.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, f"unreadable: {exc}"

    # 1. Schema.
    try:
        from sdk.agent.parser import parse_file

        node = parse_file(str(path))
    except Exception as exc:
        return False, f"schema invalid: {type(exc).__name__}: {exc}"

    # 2. Identity matches filename, so promotion cannot collide silently.
    if node.node_id != path.stem:
        return False, f"node_id {node.node_id!r} does not match filename {path.stem!r}"

    # 3. Real content, not a scaffold.
    m = _SEMANTIC_RE.search(raw)
    if not m:
        return False, "no semantic-memory section"
    text = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())
    if len(text) < MIN_SEMANTIC_MEMORY_CHARS:
        return False, f"semantic memory is {len(text)} chars — too thin to be useful"
    lowered = text.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False, "semantic memory is placeholder text"

    # 4. Embedded skill code must at least compile.
    for block in _PY_BLOCK_RE.findall(raw):
        try:
            compile(block, f"{path.name}:node-executable", "exec")
        except SyntaxError as exc:
            return False, f"embedded python does not compile: {exc}"

    return True, None


# ---------------------------------------------------------------------------
# Staging / promotion
# ---------------------------------------------------------------------------


def stage_node(node_id: str, html: str) -> Path:
    """Write `html` into staging and register it in the ledger."""
    safe_id = re.sub(r"[^a-zA-Z0-9._-]", "-", str(node_id)).strip("-") or "node"
    path = resolve_staging_dir() / f"{safe_id}.html"
    path.write_text(html, encoding="utf-8")

    ledger = _load_ledger()
    entry = ledger.get(safe_id) or {}
    entry.setdefault("first_staged", datetime.now(UTC).isoformat())
    # Content changed, so prior passes no longer describe this node.
    entry["passes"] = 0
    entry["failures"] = 0
    entry["last_error"] = None
    entry["updated"] = datetime.now(UTC).isoformat()
    ledger[safe_id] = entry
    _save_ledger(ledger)

    log.info(
        "node_staging: staged %s (needs %d passing cycle(s) to promote)",
        safe_id, promotion_passes(),
    )
    return path


def promote_node(node_id: str) -> Path | None:
    """Move a staged node into production, replacing any existing copy."""
    src = resolve_staging_dir() / f"{node_id}.html"
    if not src.is_file():
        return None
    dst = _nodes_dir() / f"{node_id}.html"
    shutil.move(str(src), str(dst))

    ledger = _load_ledger()
    ledger.pop(node_id, None)
    _save_ledger(ledger)

    log.info("node_staging: promoted %s to production", node_id)
    return dst


def quarantine_node(node_id: str, reason: str) -> Path | None:
    src = resolve_staging_dir() / f"{node_id}.html"
    if not src.is_file():
        return None
    dst = resolve_quarantine_dir() / f"{node_id}.html"
    shutil.move(str(src), str(dst))

    ledger = _load_ledger()
    entry = ledger.pop(node_id, None) or {}
    _save_ledger(ledger)
    try:
        (resolve_quarantine_dir() / f"{node_id}.reason.txt").write_text(
            f"{datetime.now(UTC).isoformat()}\n{reason}\n\nledger: {json.dumps(entry, indent=2)}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    log.warning("node_staging: quarantined %s — %s", node_id, reason)
    return dst


def list_staged() -> list[dict[str, Any]]:
    ledger = _load_ledger()
    out: list[dict[str, Any]] = []
    for path in sorted(resolve_staging_dir().glob("*.html")):
        entry = ledger.get(path.stem) or {}
        out.append(
            {
                "node_id": path.stem,
                "passes": int(entry.get("passes", 0)),
                "failures": int(entry.get("failures", 0)),
                "required_passes": promotion_passes(),
                "last_error": entry.get("last_error"),
                "first_staged": entry.get("first_staged"),
            }
        )
    return out


def run_verification_cycle(promote: bool = True) -> dict[str, Any]:
    """
    Verify every staged node once; promote or quarantine as earned.

    One call is one cycle, so ``promotion_passes()`` cycles are needed
    before anything reaches production — a node written and immediately
    regretted never gets there. Safe to call on a schedule and safe to call
    when nothing is staged.
    """
    required = promotion_passes()
    ledger = _load_ledger()
    promoted: list[str] = []
    quarantined: list[dict[str, str]] = []
    pending: list[dict[str, Any]] = []

    for path in sorted(resolve_staging_dir().glob("*.html")):
        node_id = path.stem
        entry = ledger.get(node_id) or {"first_staged": datetime.now(UTC).isoformat()}
        ok, reason = verify_node_file(path)
        entry["updated"] = datetime.now(UTC).isoformat()

        if ok:
            entry["passes"] = int(entry.get("passes", 0)) + 1
            entry["failures"] = 0
            entry["last_error"] = None
            ledger[node_id] = entry

            if promote and entry["passes"] >= required:
                _save_ledger(ledger)
                if promote_node(node_id):
                    promoted.append(node_id)
                    ledger = _load_ledger()
                    continue
            pending.append({"node_id": node_id, "passes": entry["passes"], "required": required})
        else:
            entry["passes"] = 0
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = reason
            ledger[node_id] = entry

            if entry["failures"] >= DEFAULT_MAX_FAILURES:
                _save_ledger(ledger)
                quarantine_node(node_id, reason or "repeated verification failure")
                quarantined.append({"node_id": node_id, "reason": reason or "unknown"})
                ledger = _load_ledger()
                continue
            pending.append(
                {"node_id": node_id, "passes": 0, "required": required, "last_error": reason}
            )

    _save_ledger(ledger)

    summary = {
        "promoted": promoted,
        "quarantined": quarantined,
        "pending": pending,
        "required_passes": required,
    }
    if promoted or quarantined:
        log.info(
            "node_staging: cycle complete — %d promoted, %d quarantined, %d pending",
            len(promoted), len(quarantined), len(pending),
        )
    return summary

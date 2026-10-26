"""
sdk/agent/lora_approvals.py -- the review queue in front of the LoRA corpus.

Nothing is trained on unless Steven approved it. Candidates are her chat replies (with the message each answered) and her HTML-NN
skills and memories, gathered by sdk/agent/real_usage_corpus.py, which also decides what is held back: anything the
exclude-never-redact filters reject is listed as *blocked* with its reason and can never be approved. Scrubbing here means
exclusion, not masking -- a secret redacted in place and then trained on is permanent and imperfect, an excluded item costs nothing.

A decision belongs to one exact piece of content (the id is a hash of it), so editing a node or a reply voids its approval instead
of carrying it over to different words. Decisions are stored as an HTML-NN record under the user-data root, not in a database.
"""
from __future__ import annotations

import html
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DECISIONS = ("approved", "rejected", "pending")


def _store_path() -> Path:
    from sdk.agent import node_learning as nl

    return nl.user_data_dir() / "lora" / "approvals.html"


def _read_decisions() -> dict[str, dict[str, str]]:
    """``{item id: {decision, decided_at, kind}}`` from the stored record; nothing stored means no decisions."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {li.get("data-item-id"): {"decision": li.get("data-decision") or "pending", "decided_at": li.get("data-decided-at") or "",
                                     "kind": li.get("data-kind") or ""}
            for li in doc.xpath('//ul[@id="decisions"]/li') if li.get("data-item-id")}


def _write_decisions(decisions: dict[str, dict[str, str]]) -> None:
    rows = "\n".join(
        f'    <li data-item-id="{html.escape(i)}" data-decision="{html.escape(d["decision"])}" '
        f'data-decided-at="{html.escape(d.get("decided_at", ""))}" data-kind="{html.escape(d.get("kind", ""))}"></li>'
        for i, d in sorted(decisions.items()))
    approved = sum(1 for d in decisions.values() if d["decision"] == "approved")
    document = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"UTF-8\"><title>LoRA approvals</title></head>\n<body>\n"
        '<article data-node-id="lora-approvals" data-node-type="record" data-doc-target="system.lora-approvals" '
        'data-document-purpose="record">\n  <header><h1>LoRA approvals</h1></header>\n'
        f'  <section id="semantic-memory"><p>Which replies and memories were approved for training the voice adapter: {approved} approved, '
        f'{len(decisions)} decided. An item id is a hash of its exact content.</p></section>\n'
        f'  <ul id="decisions">\n{rows}\n  </ul>\n</article>\n</body>\n</html>\n')
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(document)
    os.replace(tmp, path)


async def _gather(kind: str | None) -> list[dict[str, Any]]:
    from sdk.agent import real_usage_corpus as ruc

    items: list[dict[str, Any]] = []
    if kind in (None, "chat"):
        items += await ruc.gather_chat_candidates()
    if kind in (None, "memory"):
        items += await ruc.gather_memory_candidates()
    return items


def _status(item: dict[str, Any], decisions: dict[str, dict[str, str]]) -> str:
    return "blocked" if item["blocked"] else decisions.get(item["id"], {}).get("decision", "pending")


async def list_candidates(*, status: str | None = None, kind: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """The queue, newest first: ``{items, total, counts}``. ``status`` is pending / approved / rejected / blocked."""
    decisions = _read_decisions()
    items = await _gather(kind)
    counts = {"pending": 0, "approved": 0, "rejected": 0, "blocked": 0}
    rows: list[dict[str, Any]] = []
    for item in items:
        state = _status(item, decisions)
        counts[state] = counts.get(state, 0) + 1
        if status and state != status:
            continue
        rows.append({**item, "decision": state, "decided_at": decisions.get(item["id"], {}).get("decided_at", "")})
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"items": rows[max(0, offset):max(0, offset) + max(1, limit)], "total": len(rows), "counts": counts}


async def decide(ids: list[str], decision: str) -> dict[str, Any]:
    """Approve, reject or clear (``pending``) the given items. A blocked item cannot be approved, and an id that matches nothing
    now is refused; both come back in ``refused`` with the reason."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    by_id = {item["id"]: item for item in await _gather(None)}
    decisions = _read_decisions()
    refused: dict[str, str] = {}
    updated = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item_id in dict.fromkeys(ids):
        item = by_id.get(item_id)
        if item is None:
            refused[item_id] = "no such item"
        elif decision == "approved" and item["blocked"]:
            refused[item_id] = item["blocked"]
        elif decision == "pending":
            decisions.pop(item_id, None)
            updated += 1
        else:
            decisions[item_id] = {"decision": decision, "decided_at": now, "kind": item["kind"]}
            updated += 1
    if updated:
        _write_decisions(decisions)
    return {"updated": updated, "refused": refused}


def approved_count() -> int:
    """How many items are on record as approved. A count of decisions, not of what would survive the filters today."""
    return sum(1 for d in _read_decisions().values() if d["decision"] == "approved")


async def approved_items() -> list[dict[str, Any]]:
    """What the trainer may use: approved, still present with the same content, and passing the filters *now* (gathering runs them
    again, so a tightened rule or a swapped node drops an item that was approved earlier). Oldest first."""
    decisions = _read_decisions()
    approved = [i for i in await _gather(None) if not i["blocked"] and decisions.get(i["id"], {}).get("decision") == "approved"]
    approved.sort(key=lambda r: r["created_at"])
    return approved

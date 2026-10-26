"""
sdk/agent/lora_dataset.py -- the versioned dataset the LoRA trainer is given.

Built only from items Steven approved (sdk/agent/lora_approvals.py), each checked again against the exclude-never-redact filters at
build time. Every item is a message and her reply to it; a memory is asked for by name. The set is split once into train and
held-out, by a hash of the item id, so an item keeps its split in every later version: a held-out item never drifts into training
and quietly flatters the eval.

Stored under ``<user data>/lora/datasets/vNNNN/`` (the user-data root is D: on Kagura) as HTML-NN: ``train/corpus.html`` and
``heldout/corpus.html`` in the shape scripts/train_voice_adapter.py already reads, and ``manifest.html``, a record carrying the item
list and the id hash. A version is never rewritten. Building from an approved set identical to the latest version reuses it.

Her replies are written without the ``[Emotional State: X]`` label that memory transcripts carry: replies reach the user as written,
so the label is not part of what she should learn to say. The derived emotion is kept in the manifest for provenance.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HELDOUT_BUCKETS = 5  # one item in five is held out
MIN_FOR_HELDOUT = 5  # below this a set is too small to hold anything out, so one item is forced out anyway once there are this many


def _datasets_dir() -> Path:
    from sdk.agent import node_learning as nl

    return nl.user_data_dir() / "lora" / "datasets"


def _list_versions() -> list[str]:
    root = _datasets_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"v\d{4}", p.name) and (p / "manifest.html").is_file())


def _bucket(item_id: str) -> int:
    return int(hashlib.sha256(f"split\0{item_id}".encode()).hexdigest()[:8], 16)


def _heldout(item_id: str) -> bool:
    return _bucket(item_id) % HELDOUT_BUCKETS == 0


def _render_pair(prompt: str, reply: str) -> str:
    """The message and her reply as the two turns the trainer reads. Escaped, so nothing in a reply can open a turn of its own."""
    return (f'  <div class="turn" data-speaker="user">\n    <p>{html.escape(prompt)}</p>\n  </div>\n'
            f'  <div class="turn" data-speaker="tala">\n    <p>{html.escape(reply)}</p>\n  </div>')


def _corpus_page(era: str, pairs: list[str]) -> str:
    return ('<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8"></head>\n<body>\n'
            f'<article data-era="{html.escape(era)}">\n' + "\n\n".join(pairs) + "\n</article>\n</body>\n</html>\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _manifest_page(version: str, created: str, digest: str, items: list[dict[str, Any]]) -> str:
    payload = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    train = sum(1 for i in items if i["split"] == "train")
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8"><title>LoRA dataset</title></head>\n<body>\n'
        f'<article data-node-id="lora-dataset-{version}" data-node-type="record" data-doc-target="system.lora-dataset" '
        f'data-document-purpose="record" data-dataset-version="{version}" data-dataset-hash="{digest}" data-generated-at="{created}">\n'
        f'  <header><h1>LoRA dataset {version}</h1></header>\n'
        f'  <section id="semantic-memory"><p>{len(items)} approved items ({train} train, {len(items) - train} held out), built {created}. '
        f'Everything here was approved before it was built and passed the safety filters again at build time.</p></section>\n'
        f'  <script type="application/json" id="dataset-items">{payload}</script>\n</article>\n</body>\n</html>\n')


def load_manifest(version: str | None = None) -> dict[str, Any] | None:
    """``{version, hash, created, items, dir}`` for a version (the latest when None), or None if there is none."""
    from lxml import html as lxml_html

    versions = _list_versions()
    version = version or (versions[-1] if versions else None)
    if version is None or version not in versions:
        return None
    path = _datasets_dir() / version / "manifest.html"
    try:
        doc = lxml_html.fromstring(path.read_text(encoding="utf-8"))
        article = doc.xpath("//article")[0]
        items = json.loads(doc.xpath('//script[@id="dataset-items"]')[0].text or "[]")
    except (OSError, ValueError, IndexError):
        return None
    return {"version": version, "hash": article.get("data-dataset-hash") or "", "created": article.get("data-generated-at") or "",
            "items": items, "dir": str(path.parent)}


async def build_dataset(*, now: datetime | None = None) -> dict[str, Any]:
    """Build the next dataset version from what is approved. Never raises; the result says what happened."""
    from sdk.agent import lora_approvals
    from sdk.agent import real_usage_corpus as ruc

    approved = await lora_approvals.approved_items()
    if not approved:
        return {"success": False, "error": "no approved items: approve some replies or memories in the review queue first"}

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in approved:
        if item.get("blocked") or not ruc._passes_safety_filter(item["reply"]) or not ruc._passes_safety_filter(item["prompt"]):
            dropped += 1
            continue
        kept.append(item)
    if not kept:
        return {"success": False, "error": "every approved item failed the safety filters when the dataset was built", "dropped": dropped}

    splits = {i["id"]: "heldout" if _heldout(i["id"]) else "train" for i in kept}
    if len(kept) >= MIN_FOR_HELDOUT and "heldout" not in splits.values():
        splits[min(kept, key=lambda i: _bucket(i["id"]))["id"]] = "heldout"
    if len(kept) >= 2 and "train" not in splits.values():
        splits[max(kept, key=lambda i: _bucket(i["id"]))["id"]] = "train"

    digest = hashlib.sha256("\n".join(sorted(i["id"] for i in kept)).encode()).hexdigest()[:16]
    latest = load_manifest()
    counts = {"train": sum(1 for s in splits.values() if s == "train"), "heldout": sum(1 for s in splits.values() if s == "heldout")}
    if latest and latest["hash"] == digest:
        return {"success": True, "version": latest["version"], "hash": digest, "reused": True, "dropped": dropped, **counts,
                "dir": latest["dir"]}

    versions = _list_versions()
    version = f"v{int(versions[-1][1:]) + 1:04d}" if versions else "v0001"
    created = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    root = _datasets_dir() / version
    records: list[dict[str, Any]] = []
    pairs: dict[str, list[str]] = {"train": [], "heldout": []}
    for item in kept:
        split = splits[item["id"]]
        emotion = ruc._derive_emotion(ruc._parse_timestamp(item.get("created_at") or ""))
        records.append({"id": item["id"], "kind": item["kind"], "title": item["title"], "prompt": item["prompt"],
                        "reply": item["reply"], "emotion": emotion, "created_at": item.get("created_at", ""), "split": split})
        pairs[split].append(_render_pair(item["prompt"], item["reply"]))
    for split, rendered in pairs.items():
        _atomic_write(root / split / "corpus.html", _corpus_page(f"approved-{split}", rendered))
    _atomic_write(root / "manifest.html", _manifest_page(version, created, digest, records))  # last: a version without it does not exist
    log.info("lora_dataset: built %s (%d train, %d held out, %d dropped)", version, counts["train"], counts["heldout"], dropped)
    return {"success": True, "version": version, "hash": digest, "reused": False, "dropped": dropped, **counts, "dir": str(root)}

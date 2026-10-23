"""
sdk.agent.chain_tools -- read-only chain tools Tala can call.

Her chain used to be reachable only through side effects: documents anchored
as a by-product of other work, and a ``sync_blockchain`` tool that reconciles
a shared chain. She could not ask "is my ledger healthy?", "what was anchored
last?" or "is this record really on the chain?". These four tools answer
exactly that, and nothing else:

* ``chain_status``  -- is the local node up, how tall, does it validate
* ``chain_recent``  -- the newest blocks, summarised
* ``chain_read``    -- one transaction by id, summarised
* ``chain_verify``  -- re-verify the whole ledger independently, in Python

All four are read-only and never raise: a node that is down is an answer
("not reachable, here is where I looked"), not an error. None returns a
document body. Anchored documents are encrypted with this install's key and
the chain is readable by every peer on it, so a tool that echoed bodies back
into a prompt would be publishing content; they report that a document exists
and its type, never its contents. Writing to the chain stays where it was
(the anchoring that already happens and the gated paths), not here.

The node's address comes from ``local_chain_base_url`` -- the same variables
the node itself reads -- so a moved port is followed rather than missed.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from sdk.agent.ghost_persistence import local_chain_base_url, local_chain_data_dir
from sdk.agent.tools import register_tool

#: The node recomputes full validity on every /chain/status call (about 1.6 s
#: at 8000 blocks), so a short timeout would report a healthy node as down.
_TIMEOUT_S = 15.0
_MAX_RECENT = 50
_MAX_CHAIN_FILE_BYTES = 256 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _fetch_json(path: str) -> Any:
    with urllib.request.urlopen(local_chain_base_url() + path, timeout=_TIMEOUT_S) as response:
        return json.loads(response.read())


async def _get(path: str) -> Any:
    return await asyncio.to_thread(_fetch_json, path)


def _unreachable(exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "reachable": False,
        "endpoint": local_chain_base_url(),
        "error": f"{type(exc).__name__}: {exc}",
        "hint": "The local chain node is not answering. Start Tala (it starts the node), or check "
                "TALA_CHAIN_BIND_PORT if the node was moved off port 8332.",
    }


def _short(value: Any, size: int = 12) -> str:
    return str(value or "")[:size]


def _signer(block: dict[str, Any]) -> str | None:
    key = block.get("public_key")
    if not key:
        return None
    from python.harness.chain_verify import fingerprint

    try:
        return fingerprint(key)
    except Exception:
        return None


def _transaction_summary(tx: dict[str, Any]) -> dict[str, Any]:
    metadata = tx.get("metadata") if isinstance(tx.get("metadata"), dict) else {}
    document = tx.get("document")
    summary: dict[str, Any] = {
        "id": tx.get("id"),
        "type": tx.get("type"),
        "creator": tx.get("creator"),
        "counterparty": tx.get("counterparty"),
        "hash": _short(tx.get("hash")),
        "signed": bool(tx.get("signature")),
        "has_document": bool(document),
        "document_type": metadata.get("document_type"),
    }
    if document:
        summary["document_encrypted"] = bool(isinstance(document, dict) and document.get("encrypted"))
    return summary


def _block_summary(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": block.get("index"),
        "timestamp": block.get("timestamp"),
        "hash": _short(block.get("hash")),
        "signer": _signer(block),
        "transactions": [_transaction_summary(t) for t in block.get("transactions") or []
                         if isinstance(t, dict)],
    }


@register_tool(
    name="chain_status",
    description=(
        "Check the health of your local blockchain node: whether it is reachable, its block height, "
        "whether the whole chain validates, and its newest block. Read-only."
    ),
)
async def chain_status() -> dict[str, Any]:
    """
    NAME
           chain_status - is the local chain node up, and is the ledger valid

    SYNOPSIS
           chain_status() -> dict

    DESCRIPTION
           Asks the node for its status. Reports ``reachable``, ``height``,
           ``is_valid`` (the node's own full recheck), ``latest_block_hash``
           (first 12 characters), ``pending_transactions`` and ``peer_count``.
           A node that is down returns ``reachable: False`` with a hint; it
           never raises.
    """
    try:
        status = await _get("/chain/status")
    except Exception as exc:
        return _unreachable(exc)
    return {
        "success": True,
        "reachable": True,
        "endpoint": local_chain_base_url(),
        "height": status.get("height"),
        "is_valid": status.get("isValid"),
        "latest_block_hash": _short(status.get("latestBlockHash")),
        "pending_transactions": status.get("pendingTxCount"),
        "peer_count": status.get("peerCount"),
    }


@register_tool(
    name="chain_recent",
    description=(
        "List the newest blocks on your local blockchain (default 10, at most 50): each block's index, time, "
        "signer, and its transactions (id, type, creator, whether it carries a document). Never returns "
        "document contents. Read-only."
    ),
)
async def chain_recent(limit: int = 10) -> dict[str, Any]:
    """
    NAME
           chain_recent - what was anchored most recently

    SYNOPSIS
           chain_recent(limit: int = 10) -> dict

    DESCRIPTION
           ``limit`` is clamped to 1..50. Returns the newest blocks, oldest
           first, as summaries. Document bodies are never included.
    """
    try:
        count = max(1, min(int(limit), _MAX_RECENT))
    except (TypeError, ValueError):
        count = 10
    try:
        blocks = await _get(f"/chain/blocks?limit={count}")
    except Exception as exc:
        return _unreachable(exc)
    summaries = [_block_summary(b) for b in blocks if isinstance(b, dict)]
    return {"success": True, "reachable": True, "count": len(summaries), "blocks": summaries}


@register_tool(
    name="chain_read",
    description=(
        "Look up one transaction on your local blockchain by its id and say whether it is really on-chain: "
        "its type, creator, counterparty, block, and whether it carries a document. Never returns document "
        "contents. Read-only."
    ),
)
async def chain_read(tx_id: str) -> dict[str, Any]:
    """
    NAME
           chain_read - is this record on the chain, and what is it

    SYNOPSIS
           chain_read(tx_id: str) -> dict

    DESCRIPTION
           ``tx_id`` is the transaction id (for example ``node-create`` ids
           such as ``node-<name>``, ``ghost_commit_<task>`` or
           ``peer-directory:<wallet>``). Returns ``found``, a summary of the
           transaction and its metadata, and the block it sits in.
    """
    tx_id = str(tx_id or "").strip()
    if not _ID.match(tx_id):
        return {"success": False, "error": "tx_id must be 1-200 characters of letters, digits, _ . : or -"}
    try:
        result = await _get(f"/chain/tx/{urllib.parse.quote(tx_id, safe='')}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"success": True, "reachable": True, "found": False, "tx_id": tx_id}
        return _unreachable(exc)
    except Exception as exc:
        return _unreachable(exc)

    tx = result.get("transaction") if isinstance(result, dict) and "transaction" in result else result
    block = result.get("block") if isinstance(result, dict) else None
    metadata = tx.get("metadata") if isinstance(tx, dict) and isinstance(tx.get("metadata"), dict) else {}
    rendered = json.dumps(metadata, default=str)
    return {
        "success": True,
        "reachable": True,
        "found": True,
        "transaction": _transaction_summary(tx) if isinstance(tx, dict) else {},
        "metadata": metadata if len(rendered) <= 2000 else {"keys": sorted(metadata), "note": "metadata too large to show"},
        "block_index": block.get("index") if isinstance(block, dict) else None,
    }


def _chain_dir() -> Path:
    """Where the node keeps chain.json -- the resolver the admin-secret reader and anchoring share."""
    return local_chain_data_dir()


def _verify_file(chain_file: Path) -> dict[str, Any]:
    from python.harness.chain_verify import verify

    blocks = json.loads(chain_file.read_text(encoding="utf-8"))
    permitted = None
    authorities_file = chain_file.parent / "authorities.json"
    if authorities_file.is_file():
        entries = json.loads(authorities_file.read_text(encoding="utf-8")).get("authorities", [])
        permitted = {e["fingerprint"] for e in entries if e.get("fingerprint")}
    verdict = verify(blocks, 20, permitted)
    return {
        "success": True,
        "valid": verdict.valid,
        "height": verdict.height,
        "signed_blocks": verdict.signed,
        "unsigned_blocks": verdict.unsigned,
        "problems": verdict.problems,
        "signers_checked_against_authority_set": permitted is not None,
    }


@register_tool(
    name="chain_verify",
    description=(
        "Independently re-verify your whole local blockchain from its file: every block's hash, the links "
        "between blocks, every signature, and that each signer is a permitted authority. Reports whether it "
        "is valid and lists any problems. Slower than chain_status; read-only."
    ),
)
async def chain_verify() -> dict[str, Any]:
    """
    NAME
           chain_verify - audit the ledger without trusting the node

    SYNOPSIS
           chain_verify() -> dict

    DESCRIPTION
           Reads ``chain.json`` directly and runs the same verifier the
           shared-chain import uses, so it agrees with the node only if the
           ledger genuinely is sound. Reports ``valid``, ``height``, signed and
           unsigned block counts and up to 20 problems. Refuses a file over
           256 MB rather than loading it.
    """
    chain_file = _chain_dir() / "chain.json"
    if not chain_file.is_file():
        return {"success": False, "error": "no chain.json found", "looked_in": chain_file.parent.name}
    if chain_file.stat().st_size > _MAX_CHAIN_FILE_BYTES:
        return {"success": False, "error": "chain.json is larger than 256 MB; refusing to load it"}
    try:
        return await asyncio.to_thread(_verify_file, chain_file)
    except Exception as exc:
        return {"success": False, "error": f"could not verify: {type(exc).__name__}: {exc}"}

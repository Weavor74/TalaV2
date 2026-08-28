"""
Index the existing chain as Ghost.

Steven's decision (2026-08-28): the chain that exists -- 2,748 blocks in
``blockchain/data/chain.json`` -- is the Ghost chain, kept as it is. The Real
chain starts empty at its own genesis.

That is the right call and the transactions bear it out. All 2,747
``node-create`` entries were written by this machine anchoring its own
documents, and the single ``mint`` created the local balance. Every one of them
carries a counterparty -- ``blockchain-ledger`` or ``tala-network`` -- that
names the chain itself rather than a second party. Not one records an
interaction with anybody else, which is precisely the definition of Ghost.

This writes no blocks and moves nothing. It only fills in the index that says
which chain each existing transaction belongs to, so ``chain.summary`` can
answer the question the ledger could not answer before.

    python scripts/backfill_chain_ledger.py            # report only
    python scripts/backfill_chain_ledger.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHAIN_FILE = Path(__file__).resolve().parents[1] / "blockchain" / "data" / "chain.json"


def _blocks() -> list[dict]:
    if not CHAIN_FILE.exists():
        return []
    data = json.loads(CHAIN_FILE.read_text(encoding="utf-8"))
    return data.get("blocks", []) if isinstance(data, dict) else data


def _timestamp(block: dict, transaction: dict):
    raw = transaction.get("timestamp") or block.get("timestamp")
    if isinstance(raw, (int, float)):
        # Milliseconds if it is far too large to be seconds.
        seconds = raw / 1000.0 if raw > 1e11 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


async def main(apply: bool) -> int:
    from python.harness import chains, db

    blocks = _blocks()
    if not blocks:
        print(f"no chain found at {CHAIN_FILE}")
        return 1

    # The entry id comes from the block position, which is the only unique
    # thing in the chain -- the transaction "id" is a document identity and
    # repeats. It also makes re-running this a no-op rather than a duplicate.
    ghost, real, kinds = [], [], Counter()
    for position, block in enumerate(blocks):
        index_of = block.get("index", position)
        for offset, transaction in enumerate(block.get("transactions", [])):
            kind = str(transaction.get("type") or "unknown")
            counterparty = str(transaction.get("counterparty") or "")
            kinds[(kind, counterparty)] += 1
            tx_id = str(transaction.get("id") or "")
            if not tx_id:
                continue
            row = (
                f"block-{index_of}-{offset}", tx_id, kind, counterparty,
                str(transaction.get("hash") or "")[:512],
                _timestamp(block, transaction),
            )
            (real if chains.is_external(kind, counterparty) else ghost).append(row)

    print(f"{len(blocks)} block(s) in {CHAIN_FILE.name}")
    for (kind, counterparty), count in kinds.most_common():
        verdict = "REAL" if chains.is_external(kind, counterparty) else "ghost"
        print(f"  {count:>6}  {kind:<14} counterparty={counterparty:<20} -> {verdict}")

    if real:
        # Nothing in the existing chain should classify as external. If
        # something does, stop: publishing local history to a shared chain is
        # not a mistake that can be undone afterwards.
        print(f"\nREFUSING: {len(real)} transaction(s) classify as external.")
        print("The existing chain is the Ghost chain in its entirety. Check the")
        print("classifier before writing anything.")
        return 2

    if not apply:
        print(f"\nwould index {len(ghost)} transaction(s) as Ghost. "
              f"Re-run with --apply.")
        return 0

    await db.init_pool()
    try:
        written = 0
        for entry_id, tx_id, kind, counterparty, document_ref, occurred in ghost:
            await db.execute(
                "INSERT INTO chain_ledger (entry_id, tx_id, chain, type, "
                "counterparty, document, metadata, occurred_at, pending) "
                "VALUES ($1, $2, 'ghost', $3, $4, $5, $6::jsonb, $7, FALSE) "
                "ON CONFLICT (entry_id) DO NOTHING",
                entry_id, tx_id, kind, counterparty, document_ref,
                json.dumps({"backfilled": True}), occurred,
                caller="backfill_chain_ledger",
            )
            written += 1
        indexed = await db.fetchval(
            "SELECT COUNT(*) FROM chain_ledger WHERE chain = 'ghost'")
        print(f"\nindexed {written} transaction(s); "
              f"{indexed} Ghost row(s) now in chain_ledger")
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the index rows (default: report only)")
    raise SystemExit(asyncio.run(main(parser.parse_args().apply)))

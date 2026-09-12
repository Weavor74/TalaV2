"""
NAME
       record_stability_error - Record self-correction for missing System-Stability index

SYNOPSIS
       uv run python scripts/record_stability_error.py

DESCRIPTION
       Checks the local ledger for the 'System-Stability' index. Since the index
       is missing from the local environment, it flags the task for
       'Self-Correction' and documents the lack of information in the local
       Ghost Blockchain.
"""

from __future__ import annotations

import sys
import asyncio
import json
from pathlib import Path

# Ensure we can import python.harness.db and sdk.agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

async def main() -> None:
    """
    NAME
           main - Check stability index and log missing info
           
    SYNOPSIS
           asyncio.run(main())
           
    DESCRIPTION
           Checks the DB for system stability settings. If absent, logs
           and writes an anchoring transaction to the local blockchain.
    """
    print("[Stability] Auditing System-Stability Index...")

    from python.harness.db import init_pool, close_pool, log_to_db, fetchrow
    from sdk.agent.anchor import canonicalize_and_anchor

    await init_pool()

    try:
        # Query client_settings or agent_log for stability index
        row = await fetchrow(
            "SELECT value FROM client_settings WHERE key = 'system.stability_index'"
        )
        
        stability_value = None
        if row:
            val = row["value"]
            if isinstance(val, str):
                val = json.loads(val)
            stability_value = val.get("index") if isinstance(val, dict) else val

        if stability_value is None:
            # Missing index -> Trigger Self-Correction
            print("[Self-Correction] 'System-Stability' index is missing from local audit logs!")
            
            # 1. Document lack of information on local blockchain
            print("[Stability] Anchoring Self-Correction event to local Ghost Blockchain...")
            node_id = "self_correction_missing_system_stability"
            content_html = "<div>Self-Correction: Missing System-Stability index in local audit log. Index is required to be above 85% to initiate processes.</div>"
            state_data = {
                "event_type": "Self-Correction",
                "reason": "Missing System-Stability index in local audit log",
                "required_threshold": 0.85,
                "current_value": None
            }
            
            chaincode_id = "unanchored"
            try:
                core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
                    node_id=node_id,
                    content_html=content_html,
                    state_data=state_data,
                    document_type="self-correction",
                    creator="tala-self-auditor"
                )
                print(f"[Stability] Anchored to local blockchain. Chaincode ID: {chaincode_id}")
            except Exception as anchor_exc:
                print(f"[Warning] Failed to anchor to blockchain: {anchor_exc}")

            # 2. Log Self-Correction warning to local database ledger
            log_msg = "Self-Correction: Process initiation halted due to missing System-Stability index in local audit log. (Threshold: 85%)"
            await log_to_db(
                level="ERROR",
                message=log_msg,
                trace={
                    "event_type": "Self-Correction",
                    "reason": "Missing System-Stability index",
                    "chaincode_id": chaincode_id
                }
            )
            print("[Stability] Logged Self-Correction error to agent_log database table.")
            sys.exit(1)
        else:
            print(f"[Stability] System-Stability index is {stability_value * 100}%.")
            if stability_value < 0.85:
                print(f"[Error] System-Stability index ({stability_value * 100}%) is below required 85% threshold. Halting process initiation.")
                sys.exit(1)
            else:
                print("[Stability] Stability check passed. Process initiation allowed.")
    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(main())

"""
NAME
       sync_blockchain - Orchestrates synchronization between local and remote blockchains

SYNOPSIS
       uv run python scripts/sync_blockchain.py [options]

DESCRIPTION
       sync_blockchain performs a hash-integrity check between the local
       Ghost Blockchain and the remote Main (Real) Blockchain by calling
       pre_flight_reconciliation.
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

# Ensure we can import python.harness and sdk.agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdk.agent.ghost_persistence import pre_flight_reconciliation

async def main() -> None:
    """
    NAME
           main - Run the synchronization sequence
           
    SYNOPSIS
           asyncio.run(main())
           
    DESCRIPTION
           Triggers the pre-flight block reconciliation process.
    """
    print("[Sync] Initiating pre-flight reconciliation bridge...")
    success = await pre_flight_reconciliation()
    if success:
        print("[Sync] Pre-flight reconciliation check completed successfully.")
    else:
        print("[Sync] Pre-flight reconciliation failed or was unreachable.")

if __name__ == "__main__":
    asyncio.run(main())

"""
reindex_nodes.py - Auto-generated documentation base.
"""

import asyncio
import os
import sys
from pathlib import Path

# add path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python.harness import db
from python.harness.memory import handle_memory_reindex_node

async def reindex():
    await db.init_pool()
    
    # Wipe the old memory index for nodes first
    await db.execute("DELETE FROM memory_index WHERE source_type IN ('concept', 'memory', 'skill', 'persona', 'workflow', 'phase_documentation', 'rule')")
    
    nodes_dir = Path("D:/src/TalaV2/nodes")
    for f in nodes_dir.glob("*.html"):
        res = await handle_memory_reindex_node({"node_path": str(f)})
        print(f"Reindexed {f.name}: {res}")
        
    await db.close_pool()

if __name__ == "__main__":
    asyncio.run(reindex())

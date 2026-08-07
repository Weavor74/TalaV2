"""
clean_db.py - Auto-generated documentation base.
"""

import asyncio
import uuid
import sys
from pathlib import Path

# Adjust the python path to load db.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from python.harness import db
from sdk.agent.documentation_nodes import resolve_active_nodes_dir

async def clean_database():
    nodes_dir = resolve_active_nodes_dir()
    if not nodes_dir.exists():
        print(f"Nodes directory not found: {nodes_dir}")
        return

    # Get all node UUIDs from the DB
    try:
        await db.init_pool()
        db_nodes = await db.fetch("SELECT id FROM nodes")
        
        valid_uuids = set()
        
        # Scan filesystem for node files to extract their UUIDs
        for html_file in nodes_dir.glob("*.html"):
            try:
                # UUID is often the basename (e.g. 550e8400-e29b-41d4-a716-446655440000.html)
                basename = html_file.stem
                if "-" in basename:
                    # Sometimes they have prefixes like 'concept-distilled-collection-...'
                    # But the DB primary key requires valid UUID string if it's stored as UUID
                    pass
                
                # Check the file contents for data-node-id
                content = html_file.read_text(encoding="utf-8")
                
                # Simple extraction, it's safer to extract with regex
                import re
                match = re.search(r'data-node-id=[\'"]([^\'"]+)[\'"]', content)
                if match:
                    valid_uuids.add(match.group(1))
                else:
                    # Try to parse the UUID from filename if it's a raw UUID
                    try:
                        valid_uuids.add(str(uuid.UUID(html_file.stem)))
                    except ValueError:
                        pass
            except Exception as e:
                print(f"Error reading {html_file}: {e}")

        # Delete from DB any node ID that isn't on disk
        deleted_count = 0
        for node in db_nodes:
            node_id = str(node["id"])
            if node_id not in valid_uuids:
                print(f"Deleting orphaned node from DB: {node_id}")
                await db.execute("DELETE FROM nodes WHERE id = $1", node_id)
                await db.execute("DELETE FROM memory_index WHERE ref_id = $1", node_id)
                deleted_count += 1
                
        print(f"Cleanup complete. Deleted {deleted_count} orphaned nodes from DB.")
    finally:
        await db.close_pool()

if __name__ == "__main__":
    asyncio.run(clean_database())

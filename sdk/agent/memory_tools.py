"""
sdk.agent.memory_tools — Memory write and persistence tools

Extracted from tools.py to meet size ratchet.
Provides write_memory and related memory persistence functionality.
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC
from typing import Any


async def write_memory(content: str, topic: str = "user-preference") -> dict[str, Any]:
    """
    NAME
           write_memory - save a fact to durable memory
           
    SYNOPSIS
           write_memory(content: str, topic: str = "user-preference") -> dict[str, Any]
           
    DESCRIPTION
           Record a user preference, learned fact, or important insight to
           durable memory. Simpler and faster than docs.create_learning_node
           for straightforward "remember X" requests.
           
           - content: The information to remember (required)
           - topic: Optional category/topic tag (default: "user-preference")
    """
    if not content or not content.strip():
        return {"success": False, "error": "content cannot be empty"}
    
    try:
        from python.harness import db
        
        node_id = f"memory-note-{uuid.uuid4().hex[:12]}"
        timestamp = datetime.now(UTC)
        
        title = f"{topic}: {content[:60]}"
        await db.execute("""
            INSERT INTO memory_index (node_id, title, content, topic_tags, created_at)
            VALUES ($1, $2, $3, $4, $5)
        """, node_id, title, content.strip(), [topic], timestamp)
        
        return {
            "success": True,
            "node_id": node_id,
            "message": f"Saved to memory under topic '{topic}'"
        }
    except Exception as exc:
        return {"success": False, "error": f"Could not write to memory: {exc}"}

"""
local_provider.py - Auto-generated documentation base.
"""

import logging
from typing import Any

from python.harness import db
from sdk.agent.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

class LocalMemoryProvider(MemoryProvider):
    """
    Implements memory retrieval using PostgreSQL full-text search over the local Tala memory_index.
    """
    async def initialize(self) -> None:
        logger.info("LocalMemoryProvider initialized")

    async def teardown(self) -> None:
        pass

    async def search(self, query: str, sources: list[str], limit: int = 20, allow_private: bool = False) -> list[dict[str, Any]]:
        if not query:
            return []

        sql = """
            SELECT id, source_type, ref_id, title, left(content_text, 200) as snippet, trust_label, created_at,
                   (
                     ts_rank(tsvector_col, websearch_to_tsquery('english', $1))
                     * CASE source_type
                         WHEN 'profile' THEN 1.5
                         WHEN 'node' THEN 1.2
                         WHEN 'neural-node' THEN 1.2
                         WHEN 'task' THEN 1.1
                         ELSE 1.0
                       END
                     * CASE trust_label
                         WHEN 'verified' THEN 1.3
                         WHEN 'profile' THEN 1.2
                         WHEN 'recalled' THEN 1.0
                         WHEN 'private' THEN 1.1
                         WHEN 'external' THEN 0.8
                         WHEN 'unknown' THEN 0.5
                         ELSE 1.0
                       END
                     * GREATEST(0.5, EXP(-0.01 * EXTRACT(DAY FROM (NOW() - created_at))))
                   ) AS score
            FROM memory_index
            WHERE source_type = ANY($2)
              AND tsvector_col @@ websearch_to_tsquery('english', $1)
              AND (trust_label != 'private' OR $4::boolean = TRUE)
            ORDER BY score DESC
            LIMIT $3
        """
        rows = await db.fetch(sql, query, sources, limit, allow_private)
        
        results = []
        for row in rows:
            results.append({
                "id": f"mem-{row['id']}",
                "source_type": row["source_type"],
                "title": row["title"] or f"{row['source_type'].title()} Result",
                "snippet": row["snippet"] + "..." if len(row["snippet"]) == 200 else row["snippet"],
                "score": float(row["score"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                "ref": {"id": row["ref_id"]},
                "trust": row["trust_label"]
            })

        return results

    async def session_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Searches the conversation_turns table directly.
        """
        if not query:
            return []
            
        sql = """
            SELECT id, session_id, role, left(content, 200) as snippet, created_at,
                   ts_rank(to_tsvector('english', content), websearch_to_tsquery('english', $1)) as score
            FROM conversation_turns
            WHERE to_tsvector('english', content) @@ websearch_to_tsquery('english', $1)
            ORDER BY score DESC
            LIMIT $2
        """
        rows = await db.fetch(sql, query, limit)
        
        results = []
        for row in rows:
            results.append({
                "id": f"session-{row['id']}",
                "source_type": "session_turn",
                "title": f"Turn by {row['role']} in session {row['session_id']}",
                "snippet": row["snippet"] + "..." if len(row["snippet"]) == 200 else row["snippet"],
                "score": float(row["score"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                "ref": {"id": row["session_id"]},
                "trust": "verified"
            })

        return results

    async def store(self, source_type: str, ref_id: str, content: str, meta: dict[str, Any]) -> None:
        title = meta.get("title", "")
        trust_label = meta.get("trust_label", "recalled")
        emotion = meta.get("emotion")
        
        if emotion:
            if isinstance(emotion, dict):
                emotion_str = emotion.get("primary_emotion", "") or str(emotion)
            else:
                emotion_str = str(emotion)
            content = f"[Emotional Context: {emotion_str}]\n{content}"
        
        await db.execute(
            """
            INSERT INTO memory_index (source_type, ref_id, title, content_text, trust_label)
            SELECT $1, $2, $3, $4, $5
            WHERE NOT EXISTS (
                SELECT 1 FROM memory_index
                WHERE source_type = $1 AND ref_id = $2 AND content_text = $4
            )
            """,
            source_type, ref_id, title, content, trust_label
        )

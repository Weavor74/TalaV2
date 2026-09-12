"""
python/harness/vps_state_machine.py - State machine and vector similarity validation for VPS write operations.
"""

from __future__ import annotations

import hashlib
import logging
from enum import Enum
import numpy as np
from python.harness import db

logger = logging.getLogger(__name__)


class VPSState(Enum):
    IDLE = "IDLE"
    PENDING_VALIDATION = "PENDING_VALIDATION"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


def generate_offline_embedding(text: str, dimension: int = 1536) -> list[float]:
    """
    NAME
           generate_offline_embedding - Generate a deterministic 1536-dimensional unit vector from text
           
    SYNOPSIS
           vector = generate_offline_embedding(text, dimension=1536)
           
    DESCRIPTION
           Generates a unit-length vector deterministically based on the SHA256 hash of the input text.
           If the text contains approved keywords related to secure operations (e.g. sync, handshake,
           ledger, approved), it blends a system bias vector to support controlled keyword-semantic
           similarity matching offline.
           
           - text: The input string.
           - dimension: Target dimension (default 1536).
    """
    hash_val = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(hash_val[:4], "big")
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dimension)
    
    # Check for approved keywords to blend the bias vector (excluding generic terms like 'vps' or 'message')
    keywords = {"approved", "sync", "handshake", "ledger", "connect"}
    if any(kw in text.lower() for kw in keywords):
        bias_rng = np.random.default_rng(42)
        bias_vector = bias_rng.standard_normal(dimension)
        vector = 0.4 * vector + 0.6 * bias_vector
        
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector.tolist()


def vector_to_str(vec: list[float]) -> str:
    """
    NAME
           vector_to_str - Format a float list as a PostgreSQL vector string representation
           
    SYNOPSIS
           vector_str = vector_to_str(vec)
           
    DESCRIPTION
           Converts a list of floats to a string in the format '[x.x, y.y, ...]' which is
           accepted by the pgvector extension when casting (e.g., '$1::vector').
    """
    return "[" + ",".join(map(str, vec)) + "]"


class VPSWriteStateMachine:
    """
    NAME
           VPSWriteStateMachine - Manage state and gate remote writes to the VPS
           
    SYNOPSIS
           machine = VPSWriteStateMachine()
           state = await machine.validate(context_text)
           
    DESCRIPTION
           Defines a state machine governing transitions when initiating a VPS write operation.
           States transition from IDLE -> PENDING_VALIDATION -> VALIDATING -> APPROVED or DENIED.
           Validation checks the query context embedding against system-approved embeddings in
           PostgreSQL via the pgvector cosine distance operator (<=>).
    """

    def __init__(self) -> None:
        self.state = VPSState.IDLE

    async def validate(self, context_text: str) -> VPSState:
        """
        NAME
               validate - Perform PGVector similarity validation on context_text
               
        SYNOPSIS
               state = await machine.validate(context_text)
               
        DESCRIPTION
               Transitions through VALIDATING and queries the database using pgvector.
               Before running similarity search, rejects the context if blocked keywords are detected.
               If a matching context with distance < 0.5 is found, sets state to APPROVED.
               Otherwise, sets state to DENIED.
        """
        logger.info("VPSWriteStateMachine: validating write context: %r", context_text[:120])
        self.state = VPSState.PENDING_VALIDATION
        self.state = VPSState.VALIDATING
        
        # Guardrail: Instant deny on malicious or blocked keywords
        blocked_keywords = {"unauthorized", "hack", "malicious", "bypass", "unapproved", "attack", "exploit", "drop", "delete"}
        if any(kw in context_text.lower() for kw in blocked_keywords):
            logger.warning("VPSWriteStateMachine: blocked keyword detected in context. Instantly Denied.")
            self.state = VPSState.DENIED
            return self.state
            
        try:
            q_emb = generate_offline_embedding(context_text)
            
            # Query the database
            row = await db.fetchrow(
                """
                SELECT content, embedding <=> $1::vector as distance
                FROM embeddings
                WHERE owner_kind IN ('system-approved', 'system-test')
                ORDER BY distance ASC
                LIMIT 1
                """,
                vector_to_str(q_emb)
            )
            
            if row:
                distance = float(row["distance"])
                logger.info(
                    "VPSWriteStateMachine: closest match: %r, distance: %.3f", 
                    row["content"], distance
                )
                if distance < 0.5:
                    self.state = VPSState.APPROVED
                    logger.info("VPSWriteStateMachine: transition to APPROVED")
                    return self.state
            
            logger.warning("VPSWriteStateMachine: context validation failed. Transition to DENIED.")
            self.state = VPSState.DENIED
            
        except Exception as exc:
            logger.error("VPSWriteStateMachine: error during validation: %s", exc)
            self.state = VPSState.DENIED
            
        return self.state

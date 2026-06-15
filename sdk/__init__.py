"""
sdk/__init__.py — Tala Cognitive Engine Room: SDK Root Package
=============================================================
Top-level package for the Tala Agent SDK.  Import from here for the
most stable, forward-compatible surface area.

Quick start
-----------
::

    from sdk.agent import (
        ActivationEngine,
        ActivationInputs,
        IngestionPipeline,
        NeuralNode,
        parse_file,
        parse_html,
    )

    node = parse_file("path/to/my-node.html")
    engine = ActivationEngine()
    score = engine.score_from_node(node, semantic_match=0.85)
"""

from __future__ import annotations

__version__: str = "0.2.0"  # Milestone 2
__author__: str = "Tala Cognitive Engine Room"

# Re-export the full agent surface for convenience
from .agent import (  # noqa: F401  (re-exports)
    ActivationEngine,
    ActivationInputs,
    AuthorityBoundaryViolation,
    CorrectionRecord,
    IngestionPipeline,
    InsufficientEscrowError,
    InsufficientTokenBalanceError,
    InvalidNodeSchemaError,
    Mem0WriteBlockedError,
    NeuralNode,
    PiiAccessBlockedError,
    RelationshipAnchor,
    SemanticSection,
    StyleCapExceededError,
    TalaBaseError,
    export_node,
    ingest_file,
    parse_file,
    parse_html,
    reconcile_contradictions,
)

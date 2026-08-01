# File: `__init__.py`

## Module Description

sdk/agent/__init__.py — Tala Cognitive Engine Room: Agent SDK Package
=====================================================================
Public surface of the ``sdk.agent`` package.

Exported names
--------------
Parser
    :func:`~sdk.agent.parser.parse_file` — parse a Neural Node from disk.
    :func:`~sdk.agent.parser.parse_html` — parse a Neural Node from a string.
    :class:`~sdk.agent.parser.NeuralNode` — the canonical node dataclass.
    :class:`~sdk.agent.parser.RelationshipAnchor` — typed relationship link.

ActivationEngine
    :class:`~sdk.agent.activation.ActivationEngine` — scoring and propagation.
    :class:`~sdk.agent.activation.ActivationInputs` — named input container.

IngestionPipeline
    :class:`~sdk.agent.ingestion.IngestionPipeline` — lifecycle facade.
    :func:`~sdk.agent.ingestion.ingest_file` — ingest a node file.
    :func:`~sdk.agent.ingestion.export_node` — export to html/json/python/n8n.
    :func:`~sdk.agent.ingestion.reconcile_contradictions` — graph reconciler.
    :class:`~sdk.agent.ingestion.SemanticSection` — extracted section unit.
    :class:`~sdk.agent.ingestion.CorrectionRecord` — reconciliation log entry.

Exceptions
    All custom exceptions in :mod:`sdk.agent.exceptions`.

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
"""
sdk/agent/__init__.py — Tala Cognitive Engine Room: Agent SDK Package
=====================================================================
Public surface of the ``sdk.agent`` package.

Exported names
--------------
Parser
    :func:`~sdk.agent.parser.parse_file` — parse a Neural Node from disk.
    :func:`~sdk.agent.parser.parse_html` — parse a Neural Node from a string.
    :class:`~sdk.agent.parser.NeuralNode` — the canonical node dataclass.
    :class:`~sdk.agent.parser.RelationshipAnchor` — typed relationship link.

ActivationEngine
    :class:`~sdk.agent.activation.ActivationEngine` — scoring and propagation.
    :class:`~sdk.agent.activation.ActivationInputs` — named input container.

IngestionPipeline
    :class:`~sdk.agent.ingestion.IngestionPipeline` — lifecycle facade.
    :func:`~sdk.agent.ingestion.ingest_file` — ingest a node file.
    :func:`~sdk.agent.ingestion.export_node` — export to html/json/python/n8n.
    :func:`~sdk.agent.ingestion.reconcile_contradictions` — graph reconciler.
    :class:`~sdk.agent.ingestion.SemanticSection` — extracted section unit.
    :class:`~sdk.agent.ingestion.CorrectionRecord` — reconciliation log entry.

Exceptions
    All custom exceptions in :mod:`sdk.agent.exceptions`.
"""

from __future__ import annotations

# -- Parser ------------------------------------------------------------------
from .parser import (
    NeuralNode,
    RelationshipAnchor,
    parse_file,
    parse_html,
)

# -- Activation Engine -------------------------------------------------------
from .activation import (
    ActivationEngine,
    ActivationInputs,
)

# -- Ingestion Pipeline ------------------------------------------------------
from .ingestion import (
    CorrectionRecord,
    IngestionPipeline,
    SemanticSection,
    export_node,
    ingest_file,
    reconcile_contradictions,
)

# -- Exceptions --------------------------------------------------------------
from .exceptions import (
    AuthorityBoundaryViolation,
    InsufficientEscrowError,
    InsufficientTokenBalanceError,
    InvalidNodeSchemaError,
    Mem0WriteBlockedError,
    PiiAccessBlockedError,
    StyleCapExceededError,
    TalaBaseError,
)

# -- Workflow Compiler --------------------------------------------------------
from .workflow_compiler import compile_workflow_to_html


__all__: list[str] = [
    # Parser
    "NeuralNode",
    "RelationshipAnchor",
    "parse_file",
    "parse_html",
    # Activation
    "ActivationEngine",
    "ActivationInputs",
    # Ingestion
    "CorrectionRecord",
    "IngestionPipeline",
    "SemanticSection",
    "export_node",
    "ingest_file",
    "reconcile_contradictions",
    # Exceptions
    "AuthorityBoundaryViolation",
    "InsufficientEscrowError",
    "InsufficientTokenBalanceError",
    "InvalidNodeSchemaError",
    "Mem0WriteBlockedError",
    "PiiAccessBlockedError",
    "StyleCapExceededError",
    "TalaBaseError",
    # Workflow Compiler
    "compile_workflow_to_html",
]

```

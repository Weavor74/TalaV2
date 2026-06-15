"""
ingestion.py — Tala Cognitive Engine Room: Neural Compilation Pipeline
======================================================================
Provides the full lifecycle for reading, structuring, reconciling, and
exporting HTML Neural Nodes.

Key Design Decisions
--------------------
* **Immutable reads**: ``ingest_file`` opens files once with
  ``Path.read_text()`` in read-only mode. The pipeline never writes to
  the source filesystem.
* **Output agnosticism**: ``export_node`` supports four serialisation
  targets — ``'html'``, ``'json'``, ``'python'``, ``'tala-flow'`` — so
  nodes can be consumed by the Tala renderer, REST APIs, Python runtimes,
  and the Tala-native workflow engine without coupling the engine to any
  single format.
* **Contradiction reconciliation**: ``reconcile_contradictions`` implements
  a *cascading modifier* pattern — when a node's semantic definition
  changes, adjacent nodes that hold conflicting definitions are identified
  and correction records are logged. No silent overwrites occur.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exceptions import InvalidNodeSchemaError
from .parser import NeuralNode, parse_file, parse_html, RelationshipAnchor


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SemanticSection:
    """
    A structured semantic unit extracted from a Neural Node document.

    Attributes
    ----------
    section_id : str
        Stable deterministic identifier (SHA-256 of ``title + content``).
    title : str
        Human-readable section heading.
    content : str
        Full text content of the section.
    tags : list[str]
        Taxonomy tags extracted from ``data-tag`` attributes or inferred
        from the section heading.
    created_at : datetime
        UTC timestamp when this section was extracted (wall-clock time of
        the ingest call, not the file modification time).
    """

    section_id: str
    title: str
    content: str
    tags: list[str]
    created_at: datetime


@dataclass(slots=True)
class CorrectionRecord:
    """
    A log entry produced by :func:`reconcile_contradictions`.

    Attributes
    ----------
    record_id : str
        UUID4 for this correction event.
    source_node_id : str
        The updated node that triggered the reconciliation.
    conflicting_node_id : str
        The adjacent node found to have a conflicting definition.
    conflict_description : str
        Human-readable explanation of the detected conflict.
    corrective_action : str
        Description of what correction was applied or recommended.
    timestamp : datetime
        UTC timestamp of the reconciliation event.
    """

    record_id: str
    source_node_id: str
    conflicting_node_id: str
    conflict_description: str
    corrective_action: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _section_id(title: str, content: str) -> str:
    """
    Compute a stable, deterministic ``section_id`` from title and content.

    Uses the first 16 hex chars of SHA-256 — long enough to be collision-
    resistant for typical node document sizes, short enough to be readable.
    """
    digest = hashlib.sha256(f"{title}\x00{content}".encode("utf-8")).hexdigest()
    return digest[:16]


def _extract_tags_from_node(node: NeuralNode) -> list[str]:
    """
    Derive tags from the node's state_data and node_type.

    Looks for a ``'tags'`` key in ``state_data``; falls back to
    ``[node_type]``.
    """
    raw = node.state_data.get("tags", [])
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    return [node.node_type]


def _node_type_to_python_class(node_type: str) -> str:
    """Convert a kebab-case node_type to a PascalCase Python class name."""
    return "".join(part.capitalize() for part in node_type.replace("-", "_").split("_"))


# ---------------------------------------------------------------------------
# Ingestion functions
# ---------------------------------------------------------------------------


def ingest_file(path: str) -> list[SemanticSection]:
    """
    Read a Neural Node HTML file exactly once (immutable read) and extract
    all semantic sections from it.

    Each ``<section>`` element inside the ``<article>`` is treated as a
    :class:`SemanticSection`.  If no ``<section>`` elements are present,
    the entire ``semantic_memory`` text of the parsed node is returned as
    a single section.

    Parameters
    ----------
    path : str
        Absolute or relative path to the ``.html`` Neural Node file.

    Returns
    -------
    list[SemanticSection]
        Ordered list of semantic sections found in the document.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    InvalidNodeSchemaError
        If the document fails schema validation.
    """
    # Immutable read — file opened exactly once via parse_file → Path.read_text
    node: NeuralNode = parse_file(path)
    now: datetime = datetime.now(tz=timezone.utc)

    # Try to extract from state_data's 'sections' key first (structured format)
    raw_sections: list[dict[str, Any]] = node.state_data.get("sections", [])
    if raw_sections and isinstance(raw_sections, list):
        sections: list[SemanticSection] = []
        for raw in raw_sections:
            if not isinstance(raw, dict):
                continue
            title: str = str(raw.get("title", "Untitled"))
            content: str = str(raw.get("content", ""))
            tags: list[str] = [str(t) for t in raw.get("tags", [])]
            sections.append(
                SemanticSection(
                    section_id=_section_id(title, content),
                    title=title,
                    content=content,
                    tags=tags or _extract_tags_from_node(node),
                    created_at=now,
                )
            )
        if sections:
            return sections

    # Fallback: treat the whole semantic_memory as one section
    title = node.state_data.get("title", node.node_id)
    content = node.semantic_memory
    return [
        SemanticSection(
            section_id=_section_id(str(title), content),
            title=str(title),
            content=content,
            tags=_extract_tags_from_node(node),
            created_at=now,
        )
    ]


# ---------------------------------------------------------------------------
# Contradiction reconciliation
# ---------------------------------------------------------------------------


def reconcile_contradictions(
    updated_node: NeuralNode,
    graph: dict[str, NeuralNode],
) -> list[CorrectionRecord]:
    """
    Cascading modifier: detect adjacent nodes with conflicting definitions
    after *updated_node* has changed.

    The reconciliation algorithm:

    1. Collect all node IDs referenced by *updated_node*'s
       :class:`~tala.agent.parser.RelationshipAnchor` list.
    2. For each adjacent node found in *graph*, compare their
       ``semantic_memory`` keywords against *updated_node*'s keywords.
    3. If an adjacent node's ``node_type`` matches ``updated_node``'s
       ``node_type`` but their top-10 keyword sets are disjoint
       (zero overlap), flag a potential contradiction.
    4. For each contradiction, produce a :class:`CorrectionRecord`
       recommending a human review or automated re-embedding.

    Parameters
    ----------
    updated_node : NeuralNode
        The node that has just been updated (e.g. after re-ingestion).
    graph : dict[str, NeuralNode]
        The full node graph keyed by ``node_id``.

    Returns
    -------
    list[CorrectionRecord]
        One record per detected conflict.  Empty list if no conflicts found.
    """
    corrections: list[CorrectionRecord] = []
    now: datetime = datetime.now(tz=timezone.utc)

    # Collect adjacent node IDs from relationship anchors
    adjacent_ids: set[str] = set()
    for anchor in updated_node.relationships:
        # href may be a path like "./concept-foo.html" — strip to bare stem
        stem = Path(anchor.href).stem if anchor.href else ""
        if stem and stem in graph:
            adjacent_ids.add(stem)
        # Also try the href verbatim as a node_id
        if anchor.href in graph:
            adjacent_ids.add(anchor.href)

    # Keyword extraction: lowercase alpha tokens of length ≥ 4
    def keywords(text: str) -> set[str]:
        import re as _re
        tokens = _re.findall(r"[a-z]{4,}", text.lower())
        # Return the 10 most-frequent (preserve order via dict)
        freq: dict[str, int] = {}
        for tok in tokens:
            freq[tok] = freq.get(tok, 0) + 1
        top10 = sorted(freq, key=lambda k: -freq[k])[:10]
        return set(top10)

    source_keywords = keywords(updated_node.semantic_memory)

    for adj_id in adjacent_ids:
        adj_node: NeuralNode = graph[adj_id]

        # Only flag same-type nodes — different types have different vocabularies
        if adj_node.node_type != updated_node.node_type:
            continue

        adj_keywords = keywords(adj_node.semantic_memory)

        # Contradiction heuristic: zero overlap in top-10 keyword sets
        overlap = source_keywords & adj_keywords
        if len(overlap) == 0 and source_keywords and adj_keywords:
            corrections.append(
                CorrectionRecord(
                    record_id=str(uuid.uuid4()),
                    source_node_id=updated_node.node_id,
                    conflicting_node_id=adj_id,
                    conflict_description=(
                        f"Nodes '{updated_node.node_id}' and '{adj_id}' share "
                        f"node_type '{updated_node.node_type}' but have zero "
                        f"keyword overlap in their semantic memories. "
                        f"Source keywords: {sorted(source_keywords)}. "
                        f"Adjacent keywords: {sorted(adj_keywords)}."
                    ),
                    corrective_action=(
                        "Recommend human review and re-embedding of both nodes. "
                        "Consider merging or explicitly marking one node as "
                        "'supersedes' the other via a relationship anchor."
                    ),
                    timestamp=now,
                )
            )

    return corrections


# ---------------------------------------------------------------------------
# Node export
# ---------------------------------------------------------------------------


def export_node(node: NeuralNode, format: str) -> str:  # noqa: A002 (shadow built-in)
    """
    Serialise *node* to the requested output format.

    Parameters
    ----------
    node : NeuralNode
        The node to serialise.
    format : str
        One of ``'html'``, ``'json'``, ``'python'``, ``'tala-flow'``.

    Returns
    -------
    str
        Serialised representation of the node.

    Raises
    ------
    ValueError
        If *format* is not one of the supported values.

    Notes
    -----
    ``'tala-flow'``
        Produces a Tala-native workflow JSON stub (schema ``tala-flow/1.0``)
        that wraps the node as a trigger → action → output pipeline.
        The stub is importable by the Tala workflow runtime.
    """
    fmt = format.lower().strip()
    match fmt:
        case "html":
            return _export_html(node)
        case "json":
            return _export_json(node)
        case "python":
            return _export_python(node)
        case "tala-flow":
            return _export_tala_flow(node)
        case _:
            raise ValueError(
                f"Unsupported export format {format!r}. "
                f"Supported formats: 'html', 'json', 'python', 'tala-flow'."
            )


# -- Format implementations --------------------------------------------------


def _relationships_to_dicts(rels: list[RelationshipAnchor]) -> list[dict[str, str]]:
    return [
        {"href": r.href, "relationship_type": r.relationship_type, "label": r.label}
        for r in rels
    ]


def _export_json(node: NeuralNode) -> str:
    """Serialise node to a compact, schema-versioned JSON object."""
    payload: dict[str, Any] = {
        "$schema": "https://tala.ai/schemas/neural-node/v1.json",
        "node_id": node.node_id,
        "node_type": node.node_type,
        "execution_authority": node.execution_authority,
        "weight_relevance": node.weight_relevance,
        "semantic_memory": node.semantic_memory,
        "relationships": _relationships_to_dicts(node.relationships),
        "state_data": node.state_data,
    }
    if node.memory_type is not None:
        payload["memory_type"] = node.memory_type
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _export_html(node: NeuralNode) -> str:
    """Regenerate a canonical HTML Neural Node document from a parsed node."""
    rels_html = "\n".join(
        f'      <li><a href="{r.href}" data-relationship-type="{r.relationship_type}">'
        f"{r.label}</a></li>"
        for r in node.relationships
    )
    state_json = json.dumps(node.state_data, indent=4, ensure_ascii=False)
    mem_type_attr = f'\n  data-memory-type="{node.memory_type}"' if node.memory_type else ""
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <meta name="tala-schema" content="neural-node/v1">
          <title>Neural Node: {node.node_id}</title>
        </head>
        <body>
        <article
          data-node-id="{node.node_id}"
          data-node-type="{node.node_type}"{mem_type_attr}
          data-execution-authority="{node.execution_authority:.4f}"
          data-weight-relevance="{node.weight_relevance:.4f}">

          <!-- ── Identity / Governance ── -->
          <header>
            <h1 id="node-title">{node.node_id}</h1>
            <p>Type: <strong>{node.node_type}</strong> |
               Authority: <strong>{node.execution_authority:.4f}</strong></p>
          </header>

          <!-- ── Semantic Memory ── -->
          <section id="semantic-memory">
            <h2>Semantic Memory</h2>
            <p>{node.semantic_memory}</p>
          </section>

          <!-- ── Relationship Anchors ── -->
          <nav id="relationship-anchors" aria-label="Node relationships">
            <h2>Relationships</h2>
            <ul>
        {rels_html if rels_html else "      <li>(none)</li>"}
            </ul>
          </nav>

          <!-- ── JSON State Block ── -->
          <script type="application/json" id="node-state">
        {state_json}
          </script>

        </article>
        </body>
        </html>
    """)


def _export_python(node: NeuralNode) -> str:
    """Emit a Python module that reconstructs the node as a frozen dataclass instance."""
    rels_repr = ",\n    ".join(
        f'RelationshipAnchor(href={r.href!r}, relationship_type={r.relationship_type!r}, label={r.label!r})'
        for r in node.relationships
    )
    state_repr = repr(node.state_data)
    class_name = _node_type_to_python_class(node.node_type)
    return textwrap.dedent(f'''\
        """
        Auto-generated Neural Node module.
        Node ID   : {node.node_id}
        Node Type : {node.node_type}
        Generated : {datetime.now(tz=timezone.utc).isoformat()}
        WARNING   : Do not edit execution_authority — it is immutable.
        """
        from __future__ import annotations
        from sdk.agent.parser import NeuralNode, RelationshipAnchor

        # Immutable execution_authority={node.execution_authority!r}
        NODE_{node.node_id.upper().replace("-", "_")} = NeuralNode(
            node_id={node.node_id!r},
            node_type={node.node_type!r},
            governance_status={node.governance_status!r},
            execution_authority={node.execution_authority!r},
            user_approval_strength={node.user_approval_strength!r},
            weight_relevance={node.weight_relevance!r},
            weight_confidence={node.weight_confidence!r},
            weight_behavior_influence={node.weight_behavior_influence!r},
            weight_governance_importance={node.weight_governance_importance!r},
            memory_authority={node.memory_authority!r},
            provider_authority={node.provider_authority!r},
            wallet_authority={node.wallet_authority!r},
            semantic_memory={node.semantic_memory!r},
            relationships=[
                {rels_repr if rels_repr else ""}
            ],
            state_data={state_repr},
            memory_type={node.memory_type!r},
        )
    ''')



def _export_tala_flow(node: NeuralNode) -> str:
    """
    Produce a Tala-native workflow JSON stub (schema ``tala-flow/1.0``).

    The stub encodes the node as a three-step pipeline:

    1. **trigger** — entry point, fires when the node activates.
    2. **action**  — executes the node's primary semantic operation.
    3. **output**  — emits the result to downstream consumers.

    The stub is importable by the Tala workflow runtime and can be
    extended with additional nodes and conditional branches.
    """
    flow_id = str(uuid.uuid4())
    trigger_id = str(uuid.uuid4())
    action_id = str(uuid.uuid4())
    output_id = str(uuid.uuid4())
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    # Derive a human-readable flow name from state_data or node_id
    flow_name: str = str(node.state_data.get("title", node.node_id))

    workflow: dict[str, Any] = {
        "schema": "tala-flow/1.0",
        "flow_id": flow_id,
        "name": flow_name,
        "nodes": [
            {
                "id": trigger_id,
                "type": "trigger",
                "label": f"Activate {node.node_id}",
                "config": {
                    "source_node_id": node.node_id,
                    "node_type": node.node_type,
                    "execution_authority": node.execution_authority,
                },
                "connections": {
                    "onSuccess": action_id,
                    "onFailure": None,
                },
            },
            {
                "id": action_id,
                "type": "action",
                "label": f"Execute {node.node_type} logic",
                "config": {
                    "weight_relevance": node.weight_relevance,
                    "semantic_memory": node.semantic_memory[:500],  # truncate for config
                    "state_data": node.state_data,
                },
                "connections": {
                    "onSuccess": output_id,
                    "onFailure": None,
                },
            },
            {
                "id": output_id,
                "type": "output",
                "label": "Emit result",
                "config": {
                    "relationships": _relationships_to_dicts(node.relationships),
                    "format": "json",
                },
                "connections": {
                    "onSuccess": None,
                    "onFailure": None,
                },
            },
        ],
        "metadata": {
            "source_node_id": node.node_id,
            "created_at": now_iso,
        },
    }

    return json.dumps(workflow, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# IngestionPipeline class
# ---------------------------------------------------------------------------


class IngestionPipeline:
    """
    High-level facade wrapping the full Neural Node ingestion lifecycle.

    Provides a single entry-point for:

    * Ingesting files into :class:`SemanticSection` lists.
    * Reconciling contradictions across a node graph.
    * Exporting nodes to various serialisation formats.

    All methods are safe to call concurrently from multiple threads
    because the pipeline holds no mutable state of its own.

    Parameters
    ----------
    graph : dict[str, NeuralNode] | None
        Optional pre-populated node graph.  If ``None``, an empty graph
        is used.  Can be updated after construction via :attr:`graph`.

    Examples
    --------
    >>> pipeline = IngestionPipeline()
    >>> sections = pipeline.ingest("path/to/node.html")
    >>> pipeline.load_node("path/to/node.html")
    >>> exported = pipeline.export(pipeline.graph["my-node"], format="json")
    """

    def __init__(self, graph: dict[str, NeuralNode] | None = None) -> None:
        self.graph: dict[str, NeuralNode] = graph if graph is not None else {}

    # ------------------------------------------------------------------

    def ingest(self, path: str) -> list[SemanticSection]:
        """
        Ingest a Neural Node file and return its semantic sections.

        Delegates to :func:`ingest_file`.

        Parameters
        ----------
        path : str
            Path to the ``.html`` Neural Node file.

        Returns
        -------
        list[SemanticSection]
        """
        return ingest_file(path)

    def load_node(self, path: str) -> NeuralNode:
        """
        Parse a Neural Node file and register it in the pipeline's graph.

        After this call, the node is accessible via ``self.graph[node_id]``.

        Parameters
        ----------
        path : str
            Path to the ``.html`` Neural Node file.

        Returns
        -------
        NeuralNode
            The parsed node (also stored in ``self.graph``).
        """
        node = parse_file(path)
        self.graph[node.node_id] = node
        return node

    def load_node_from_html(self, html_str: str) -> NeuralNode:
        """
        Parse an in-memory HTML string and register the node in the graph.

        Parameters
        ----------
        html_str : str
            Raw HTML content of a Neural Node document.

        Returns
        -------
        NeuralNode
        """
        node = parse_html(html_str)
        self.graph[node.node_id] = node
        return node

    def reconcile(
        self,
        updated_node: NeuralNode,
    ) -> list[CorrectionRecord]:
        """
        Run contradiction reconciliation for *updated_node* against the
        current pipeline graph.

        Delegates to :func:`reconcile_contradictions`.

        Parameters
        ----------
        updated_node : NeuralNode
            The node whose update may have introduced contradictions.

        Returns
        -------
        list[CorrectionRecord]
            One record per detected conflict.
        """
        return reconcile_contradictions(updated_node, self.graph)

    def export(self, node: NeuralNode, format: str) -> str:  # noqa: A002
        """
        Serialise a node to the requested format.

        Delegates to :func:`export_node`.

        Parameters
        ----------
        node : NeuralNode
            The node to export.
        format : str
            One of ``'html'``, ``'json'``, ``'python'``, ``'n8n'``.

        Returns
        -------
        str
            Serialised node content.
        """
        return export_node(node, format)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "IngestionPipeline",
    "SemanticSection",
    "CorrectionRecord",
    "ingest_file",
    "reconcile_contradictions",
    "export_node",
]

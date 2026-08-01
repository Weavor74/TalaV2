# File: `ingestion.py`

## Module Description

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

## Classes

### `class SemanticSection`
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


### `class CorrectionRecord`
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


### `class IngestionPipeline`
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

- **`def __init__(self, graph)`**
- **`def ingest(self, path)`**
  - Ingest a Neural Node file and return its semantic sections.
  
  Delegates to :func:`ingest_file`.
  
  Parameters
  ----------
  path : str
      Path to the ``.html`` Neural Node file.
  
  Returns
  -------
  list[SemanticSection]
- **`def load_node(self, path)`**
  - Parse a Neural Node file and register it in the pipeline's graph.
  
  After this call, the node is accessible via ``self.graph[node_id]``.
  
  Parameters
  ----------
  path : str
      Path to the ``.html`` Neural Node file.
  
  Returns
  -------
  NeuralNode
      The parsed node (also stored in ``self.graph``).
- **`def load_node_from_html(self, html_str)`**
  - Parse an in-memory HTML string and register the node in the graph.
  
  Parameters
  ----------
  html_str : str
      Raw HTML content of a Neural Node document.
  
  Returns
  -------
  NeuralNode
- **`def reconcile(self, updated_node)`**
  - Run contradiction reconciliation for *updated_node* against the
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
- **`def export(self, node, format)`**
  - Serialise a node to the requested format.
  
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

## Functions

### `def _section_id(title, content)`
Compute a stable, deterministic ``section_id`` from title and content.

Uses the first 16 hex chars of SHA-256 — long enough to be collision-
resistant for typical node document sizes, short enough to be readable.

### `def _extract_tags_from_node(node)`
Derive tags from the node's state_data and node_type.

Looks for a ``'tags'`` key in ``state_data``; falls back to
``[node_type]``.

### `def _node_type_to_python_class(node_type)`
Convert a kebab-case node_type to a PascalCase Python class name.

### `def ingest_file(path)`
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

### `def reconcile_contradictions(updated_node, graph)`
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

### `def export_node(node, format)`
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

### `def _relationships_to_dicts(rels)`
*No docstring provided.*

### `def _export_json(node)`
Serialise node to a compact, schema-versioned JSON object.

### `def _export_html(node)`
Regenerate a canonical HTML Neural Node document from a parsed node.

### `def _export_python(node)`
Emit a Python module that reconstructs the node as a frozen dataclass instance.

### `def _export_tala_flow(node)`
Produce a Tala-native workflow JSON stub (schema ``tala-flow/1.0``).

The stub encodes the node as a three-step pipeline:

1. **trigger** — entry point, fires when the node activates.
2. **action**  — executes the node's primary semantic operation.
3. **output**  — emits the result to downstream consumers.

The stub is importable by the Tala workflow runtime and can be
extended with additional nodes and conditional branches.

## Source Code

```python
"""\ningestion.py — Tala Cognitive Engine Room: Neural Compilation Pipeline\n======================================================================\nProvides the full lifecycle for reading, structuring, reconciling, and\nexporting HTML Neural Nodes.\n\nKey Design Decisions\n--------------------\n* **Immutable reads**: ``ingest_file`` opens files once with\n  ``Path.read_text()`` in read-only mode. The pipeline never writes to\n  the source filesystem.\n* **Output agnosticism**: ``export_node`` supports four serialisation\n  targets — ``'html'``, ``'json'``, ``'python'``, ``'tala-flow'`` — so\n  nodes can be consumed by the Tala renderer, REST APIs, Python runtimes,\n  and the Tala-native workflow engine without coupling the engine to any\n  single format.\n* **Contradiction reconciliation**: ``reconcile_contradictions`` implements\n  a *cascading modifier* pattern — when a node's semantic definition\n  changes, adjacent nodes that hold conflicting definitions are identified\n  and correction records are logged. No silent overwrites occur.\n"""\n\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport textwrap\nimport uuid\nfrom dataclasses import dataclass, field\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import Any\n\nfrom .exceptions import InvalidNodeSchemaError\nfrom .parser import NeuralNode, parse_file, parse_html, RelationshipAnchor\n\n\n# ---------------------------------------------------------------------------\n# Public dataclasses\n# ---------------------------------------------------------------------------\n\n\n@dataclass(slots=True)\nclass SemanticSection:\n    """\n    A structured semantic unit extracted from a Neural Node document.\n\n    Attributes\n    ----------\n    section_id : str\n        Stable deterministic identifier (SHA-256 of ``title + content``).\n    title : str\n        Human-readable section heading.\n    content : str\n        Full text content of the section.\n    tags : list[str]\n        Taxonomy tags extracted from ``data-tag`` attributes or inferred\n        from the section heading.\n    created_at : datetime\n        UTC timestamp when this section was extracted (wall-clock time of\n        the ingest call, not the file modification time).\n    """\n\n    section_id: str\n    title: str\n    content: str\n    tags: list[str]\n    created_at: datetime\n\n\n@dataclass(slots=True)\nclass CorrectionRecord:\n    """\n    A log entry produced by :func:`reconcile_contradictions`.\n\n    Attributes\n    ----------\n    record_id : str\n        UUID4 for this correction event.\n    source_node_id : str\n        The updated node that triggered the reconciliation.\n    conflicting_node_id : str\n        The adjacent node found to have a conflicting definition.\n    conflict_description : str\n        Human-readable explanation of the detected conflict.\n    corrective_action : str\n        Description of what correction was applied or recommended.\n    timestamp : datetime\n        UTC timestamp of the reconciliation event.\n    """\n\n    record_id: str\n    source_node_id: str\n    conflicting_node_id: str\n    conflict_description: str\n    corrective_action: str\n    timestamp: datetime\n\n\n# ---------------------------------------------------------------------------\n# Internal helpers\n# ---------------------------------------------------------------------------\n\n\ndef _section_id(title: str, content: str) -> str:\n    """\n    Compute a stable, deterministic ``section_id`` from title and content.\n\n    Uses the first 16 hex chars of SHA-256 — long enough to be collision-\n    resistant for typical node document sizes, short enough to be readable.\n    """\n    digest = hashlib.sha256(f"{title}\x00{content}".encode("utf-8")).hexdigest()\n    return digest[:16]\n\n\ndef _extract_tags_from_node(node: NeuralNode) -> list[str]:\n    """\n    Derive tags from the node's state_data and node_type.\n\n    Looks for a ``'tags'`` key in ``state_data``; falls back to\n    ``[node_type]``.\n    """\n    raw = node.state_data.get("tags", [])\n    if isinstance(raw, list):\n        return [str(t) for t in raw if t]\n    return [node.node_type]\n\n\ndef _node_type_to_python_class(node_type: str) -> str:\n    """Convert a kebab-case node_type to a PascalCase Python class name."""\n    return "".join(part.capitalize() for part in node_type.replace("-", "_").split("_"))\n\n\n# ---------------------------------------------------------------------------\n# Ingestion functions\n# ---------------------------------------------------------------------------\n\n\ndef ingest_file(path: str) -> list[SemanticSection]:\n    """\n    Read a Neural Node HTML file exactly once (immutable read) and extract\n    all semantic sections from it.\n\n    Each ``<section>`` element inside the ``<article>`` is treated as a\n    :class:`SemanticSection`.  If no ``<section>`` elements are present,\n    the entire ``semantic_memory`` text of the parsed node is returned as\n    a single section.\n\n    Parameters\n    ----------\n    path : str\n        Absolute or relative path to the ``.html`` Neural Node file.\n\n    Returns\n    -------\n    list[SemanticSection]\n        Ordered list of semantic sections found in the document.\n\n    Raises\n    ------\n    FileNotFoundError\n        If *path* does not exist.\n    InvalidNodeSchemaError\n        If the document fails schema validation.\n    """\n    # Immutable read — file opened exactly once via parse_file → Path.read_text\n    node: NeuralNode = parse_file(path)\n    now: datetime = datetime.now(tz=timezone.utc)\n\n    # Try to extract from state_data's 'sections' key first (structured format)\n    raw_sections: list[dict[str, Any]] = node.state_data.get("sections", [])\n    if raw_sections and isinstance(raw_sections, list):\n        sections: list[SemanticSection] = []\n        for raw in raw_sections:\n            if not isinstance(raw, dict):\n                continue\n            title: str = str(raw.get("title", "Untitled"))\n            content: str = str(raw.get("content", ""))\n            tags: list[str] = [str(t) for t in raw.get("tags", [])]\n            sections.append(\n                SemanticSection(\n                    section_id=_section_id(title, content),\n                    title=title,\n                    content=content,\n                    tags=tags or _extract_tags_from_node(node),\n                    created_at=now,\n                )\n            )\n        if sections:\n            return sections\n\n    # Fallback: treat the whole semantic_memory as one section\n    title = node.state_data.get("title", node.node_id)\n    content = node.semantic_memory\n    return [\n        SemanticSection(\n            section_id=_section_id(str(title), content),\n            title=str(title),\n            content=content,\n            tags=_extract_tags_from_node(node),\n            created_at=now,\n        )\n    ]\n\n\n# ---------------------------------------------------------------------------\n# Contradiction reconciliation\n# ---------------------------------------------------------------------------\n\n\ndef reconcile_contradictions(\n    updated_node: NeuralNode,\n    graph: dict[str, NeuralNode],\n) -> list[CorrectionRecord]:\n    """\n    Cascading modifier: detect adjacent nodes with conflicting definitions\n    after *updated_node* has changed.\n\n    The reconciliation algorithm:\n\n    1. Collect all node IDs referenced by *updated_node*'s\n       :class:`~tala.agent.parser.RelationshipAnchor` list.\n    2. For each adjacent node found in *graph*, compare their\n       ``semantic_memory`` keywords against *updated_node*'s keywords.\n    3. If an adjacent node's ``node_type`` matches ``updated_node``'s\n       ``node_type`` but their top-10 keyword sets are disjoint\n       (zero overlap), flag a potential contradiction.\n    4. For each contradiction, produce a :class:`CorrectionRecord`\n       recommending a human review or automated re-embedding.\n\n    Parameters\n    ----------\n    updated_node : NeuralNode\n        The node that has just been updated (e.g. after re-ingestion).\n    graph : dict[str, NeuralNode]\n        The full node graph keyed by ``node_id``.\n\n    Returns\n    -------\n    list[CorrectionRecord]\n        One record per detected conflict.  Empty list if no conflicts found.\n    """\n    corrections: list[CorrectionRecord] = []\n    now: datetime = datetime.now(tz=timezone.utc)\n\n    # Collect adjacent node IDs from relationship anchors\n    adjacent_ids: set[str] = set()\n    for anchor in updated_node.relationships:\n        # href may be a path like "./concept-foo.html" — strip to bare stem\n        stem = Path(anchor.href).stem if anchor.href else ""\n        if stem and stem in graph:\n            adjacent_ids.add(stem)\n        # Also try the href verbatim as a node_id\n        if anchor.href in graph:\n            adjacent_ids.add(anchor.href)\n\n    # Keyword extraction: lowercase alpha tokens of length ≥ 4\n    def keywords(text: str) -> set[str]:\n        import re as _re\n        tokens = _re.findall(r"[a-z]{4,}", text.lower())\n        # Return the 10 most-frequent (preserve order via dict)\n        freq: dict[str, int] = {}\n        for tok in tokens:\n            freq[tok] = freq.get(tok, 0) + 1\n        top10 = sorted(freq, key=lambda k: -freq[k])[:10]\n        return set(top10)\n\n    source_keywords = keywords(updated_node.semantic_memory)\n\n    for adj_id in adjacent_ids:\n        adj_node: NeuralNode = graph[adj_id]\n\n        # Only flag same-type nodes — different types have different vocabularies\n        if adj_node.node_type != updated_node.node_type:\n            continue\n\n        adj_keywords = keywords(adj_node.semantic_memory)\n\n        # Contradiction heuristic: zero overlap in top-10 keyword sets\n        overlap = source_keywords & adj_keywords\n        if len(overlap) == 0 and source_keywords and adj_keywords:\n            corrections.append(\n                CorrectionRecord(\n                    record_id=str(uuid.uuid4()),\n                    source_node_id=updated_node.node_id,\n                    conflicting_node_id=adj_id,\n                    conflict_description=(\n                        f"Nodes '{updated_node.node_id}' and '{adj_id}' share "\n                        f"node_type '{updated_node.node_type}' but have zero "\n                        f"keyword overlap in their semantic memories. "\n                        f"Source keywords: {sorted(source_keywords)}. "\n                        f"Adjacent keywords: {sorted(adj_keywords)}."\n                    ),\n                    corrective_action=(\n                        "Recommend human review and re-embedding of both nodes. "\n                        "Consider merging or explicitly marking one node as "\n                        "'supersedes' the other via a relationship anchor."\n                    ),\n                    timestamp=now,\n                )\n            )\n\n    return corrections\n\n\n# ---------------------------------------------------------------------------\n# Node export\n# ---------------------------------------------------------------------------\n\n\ndef export_node(node: NeuralNode, format: str) -> str:  # noqa: A002 (shadow built-in)\n    """\n    Serialise *node* to the requested output format.\n\n    Parameters\n    ----------\n    node : NeuralNode\n        The node to serialise.\n    format : str\n        One of ``'html'``, ``'json'``, ``'python'``, ``'tala-flow'``.\n\n    Returns\n    -------\n    str\n        Serialised representation of the node.\n\n    Raises\n    ------\n    ValueError\n        If *format* is not one of the supported values.\n\n    Notes\n    -----\n    ``'tala-flow'``\n        Produces a Tala-native workflow JSON stub (schema ``tala-flow/1.0``)\n        that wraps the node as a trigger → action → output pipeline.\n        The stub is importable by the Tala workflow runtime.\n    """\n    fmt = format.lower().strip()\n    match fmt:\n        case "html":\n            return _export_html(node)\n        case "json":\n            return _export_json(node)\n        case "python":\n            return _export_python(node)\n        case "tala-flow":\n            return _export_tala_flow(node)\n        case _:\n            raise ValueError(\n                f"Unsupported export format {format!r}. "\n                f"Supported formats: 'html', 'json', 'python', 'tala-flow'."\n            )\n\n\n# -- Format implementations --------------------------------------------------\n\n\ndef _relationships_to_dicts(rels: list[RelationshipAnchor]) -> list[dict[str, str]]:\n    return [\n        {"href": r.href, "relationship_type": r.relationship_type, "label": r.label}\n        for r in rels\n    ]\n\n\ndef _export_json(node: NeuralNode) -> str:\n    """Serialise node to a compact, schema-versioned JSON object."""\n    payload: dict[str, Any] = {\n        "$schema": "https://tala.ai/schemas/neural-node/v1.json",\n        "node_id": node.node_id,\n        "node_type": node.node_type,\n        "execution_authority": node.execution_authority,\n        "weight_relevance": node.weight_relevance,\n        "semantic_memory": node.semantic_memory,\n        "relationships": _relationships_to_dicts(node.relationships),\n        "state_data": node.state_data,\n    }\n    if node.memory_type is not None:\n        payload["memory_type"] = node.memory_type\n    return json.dumps(payload, indent=2, ensure_ascii=False)\n\n\ndef _export_html(node: NeuralNode) -> str:\n    """Regenerate a canonical HTML Neural Node document from a parsed node."""\n    rels_html = "\n".join(\n        f'      <li><a href="{r.href}" data-relationship-type="{r.relationship_type}">'\n        f"{r.label}</a></li>"\n        for r in node.relationships\n    )\n    state_json = json.dumps(node.state_data, indent=4, ensure_ascii=False)\n    mem_type_attr = f'\n  data-memory-type="{node.memory_type}"' if node.memory_type else ""\n    return textwrap.dedent(f"""\\n        <!DOCTYPE html>\n        <html lang="en">\n        <head>\n          <meta charset="UTF-8">\n          <meta name="tala-schema" content="neural-node/v1">\n          <title>Neural Node: {node.node_id}</title>\n        </head>\n        <body>\n        <article\n          data-node-id="{node.node_id}"\n          data-node-type="{node.node_type}"{mem_type_attr}\n          data-execution-authority="{node.execution_authority:.4f}"\n          data-weight-relevance="{node.weight_relevance:.4f}">\n\n          <!-- ── Identity / Governance ── -->\n          <header>\n            <h1 id="node-title">{node.node_id}</h1>\n            <p>Type: <strong>{node.node_type}</strong> |\n               Authority: <strong>{node.execution_authority:.4f}</strong></p>\n          </header>\n\n          <!-- ── Semantic Memory ── -->\n          <section id="semantic-memory">\n            <h2>Semantic Memory</h2>\n            <p>{node.semantic_memory}</p>\n          </section>\n\n          <!-- ── Relationship Anchors ── -->\n          <nav id="relationship-anchors" aria-label="Node relationships">\n            <h2>Relationships</h2>\n            <ul>\n        {rels_html if rels_html else "      <li>(none)</li>"}\n            </ul>\n          </nav>\n\n          <!-- ── JSON State Block ── -->\n          <script type="application/json" id="node-state">\n        {state_json}\n          </script>\n\n        </article>\n        </body>\n        </html>\n    """)\n\n\ndef _export_python(node: NeuralNode) -> str:\n    """Emit a Python module that reconstructs the node as a frozen dataclass instance."""\n    rels_repr = ",\n    ".join(\n        f'RelationshipAnchor(href={r.href!r}, relationship_type={r.relationship_type!r}, label={r.label!r})'\n        for r in node.relationships\n    )\n    state_repr = repr(node.state_data)\n    class_name = _node_type_to_python_class(node.node_type)\n    return textwrap.dedent(f'''\\n        """\n        Auto-generated Neural Node module.\n        Node ID   : {node.node_id}\n        Node Type : {node.node_type}\n        Generated : {datetime.now(tz=timezone.utc).isoformat()}\n        WARNING   : Do not edit execution_authority — it is immutable.\n        """\n        from __future__ import annotations\n        from sdk.agent.parser import NeuralNode, RelationshipAnchor\n\n        # Immutable execution_authority={node.execution_authority!r}\n        NODE_{node.node_id.upper().replace("-", "_")} = NeuralNode(\n            node_id={node.node_id!r},\n            node_type={node.node_type!r},\n            governance_status={node.governance_status!r},\n            execution_authority={node.execution_authority!r},\n            user_approval_strength={node.user_approval_strength!r},\n            weight_relevance={node.weight_relevance!r},\n            weight_confidence={node.weight_confidence!r},\n            weight_behavior_influence={node.weight_behavior_influence!r},\n            weight_governance_importance={node.weight_governance_importance!r},\n            memory_authority={node.memory_authority!r},\n            provider_authority={node.provider_authority!r},\n            wallet_authority={node.wallet_authority!r},\n            semantic_memory={node.semantic_memory!r},\n            relationships=[\n                {rels_repr if rels_repr else ""}\n            ],\n            state_data={state_repr},\n            memory_type={node.memory_type!r},\n        )\n    ''')\n\n\n\ndef _export_tala_flow(node: NeuralNode) -> str:\n    """\n    Produce a Tala-native workflow JSON stub (schema ``tala-flow/1.0``).\n\n    The stub encodes the node as a three-step pipeline:\n\n    1. **trigger** — entry point, fires when the node activates.\n    2. **action**  — executes the node's primary semantic operation.\n    3. **output**  — emits the result to downstream consumers.\n\n    The stub is importable by the Tala workflow runtime and can be\n    extended with additional nodes and conditional branches.\n    """\n    flow_id = str(uuid.uuid4())\n    trigger_id = str(uuid.uuid4())\n    action_id = str(uuid.uuid4())\n    output_id = str(uuid.uuid4())\n    now_iso = datetime.now(tz=timezone.utc).isoformat()\n... (file truncated, showing first 500 lines) ...
```

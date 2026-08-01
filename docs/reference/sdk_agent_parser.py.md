# File: `parser.py`

## Module Description

parser.py — Tala Cognitive Engine Room: HTML Neural Node Parser
===============================================================
Parses HTML Neural Node documents into strongly-typed Python dataclasses.

Security invariants
-------------------
* JSON state blocks are parsed with ``json.loads()`` exclusively.
  ``eval()``, ``exec()``, and ``ast.literal_eval()`` are NEVER used.
* The parser asserts that the parsed state block is a ``dict`` before
  returning; any other type raises ``InvalidNodeSchemaError``.
* Files are opened in read-only mode (``'r'``); the parser never writes
  to disk.

Schema invariants
-----------------
* ``data-node-id``            — required; must be non-empty
* ``data-node-type``          — required; must be non-empty
* ``data-execution-authority``— required; must parse as a float in [0.0, 1.0]

Missing any required attribute raises ``InvalidNodeSchemaError``.

## Classes

### `class RelationshipAnchor`
Represents a typed relationship link from one Neural Node to another.

Attributes
----------
href : str
    Relative or absolute path/URL of the linked node document.
relationship_type : str
    Semantic relationship label (e.g. 'depends-on', 'extends',
    'contradicts', 'authored-by').
relationship_weight : float
    The relevance weight of this specific connection edge in [0.0, 1.0].
label : str
    Human-readable display text of the anchor.


### `class NeuralNode`
A fully parsed and validated HTML Neural Node.

Fields
------
node_id : str
    Unique stable identifier for this node (from ``data-node-id``).
node_type : str
    Categorical type tag (from ``data-node-type``).
governance_status : str
    Governance status string (e.g. 'context-only', 'doctrine').
execution_authority : float
    Governance authority level in [0.0, 1.0].
user_approval_strength : float
    Approval strength in [0.0, 1.0].
weight_relevance : float
    Initial semantic relevance weight in [0.0, 1.0].
weight_confidence : float
    Confidence weight in [0.0, 1.0].
weight_behavior_influence : float
    Behavior influence weight in [0.0, 1.0].
weight_governance_importance : float
    Governance weight in [0.0, 1.0].
memory_authority : float
    Memory authority level in [0.0, 1.0].
provider_authority : float
    Provider authority level in [0.0, 1.0].
wallet_authority : float
    Wallet authority level in [0.0, 1.0].
semantic_memory : str
    Full text content of the node's semantic memory section.
relationships : list[RelationshipAnchor]
    All typed outbound relationship links declared in the document.
state_data : dict[str, Any]
    Parsed JSON state block.


## Functions

### `def _clamp(value, lo, hi)`
Return *value* clamped to [lo, hi].

### `def _parse_authority(raw, path)`
Convert *raw* string to a float and clamp to [0.0, 1.0].

Raises
------
InvalidNodeSchemaError
    If *raw* cannot be interpreted as a number.

### `def _validate_article(article, path)`
Assert that *article* carries all required attributes.

Raises
------
InvalidNodeSchemaError
    If any required attribute is absent or empty.

### `def _extract_semantic_memory(article)`
Return the text content of the element whose ``id`` is
``'semantic-memory'``, or the entire article text as fallback.

### `def _extract_relationships(article)`
Collect every ``<a data-relationship-type="...">`` anchor inside
*article* and return them as :class:`RelationshipAnchor` objects.

### `def _extract_json_block(article, script_id, path)`
*No docstring provided.*

### `def _extract_state_data(article, path)`
*No docstring provided.*

### `def _extract_executable_code(article)`
Find ``<script type="application/x-python" id="node-executable">`` inside
*article* and return its text content. Returns None if absent.

### `def _parse_tree(tree, path)`
Core parsing logic shared by :func:`parse_file` and :func:`parse_html`.

Parameters
----------
tree :
    An lxml element tree rooted at the document ``<html>`` element.
path :
    Filesystem path used for error messages (``None`` for in-memory HTML).

Returns
-------
NeuralNode
    Fully populated and validated node.

### `def parse_file(path)`
Parse an HTML Neural Node from a file on disk.

The file is opened **read-only** using ``pathlib.Path.read_text()``.
The parser never writes to disk.

Parameters
----------
path : str
    Absolute or relative filesystem path to the ``.html`` node file.

Returns
-------
NeuralNode
    Fully parsed and validated Neural Node.

Raises
------
InvalidNodeSchemaError
    If the document fails schema validation.
FileNotFoundError
    If *path* does not exist.
OSError
    If the file cannot be read.

### `def parse_html(html_str)`
Parse an HTML Neural Node from an in-memory HTML string.

Parameters
----------
html_str : str
    Raw HTML content of a Neural Node document.

Returns
-------
NeuralNode
    Fully parsed and validated Neural Node.

Raises
------
InvalidNodeSchemaError
    If the document fails schema validation.

## Source Code

```python
"""\nparser.py — Tala Cognitive Engine Room: HTML Neural Node Parser\n===============================================================\nParses HTML Neural Node documents into strongly-typed Python dataclasses.\n\nSecurity invariants\n-------------------\n* JSON state blocks are parsed with ``json.loads()`` exclusively.\n  ``eval()``, ``exec()``, and ``ast.literal_eval()`` are NEVER used.\n* The parser asserts that the parsed state block is a ``dict`` before\n  returning; any other type raises ``InvalidNodeSchemaError``.\n* Files are opened in read-only mode (``'r'``); the parser never writes\n  to disk.\n\nSchema invariants\n-----------------\n* ``data-node-id``            — required; must be non-empty\n* ``data-node-type``          — required; must be non-empty\n* ``data-execution-authority``— required; must parse as a float in [0.0, 1.0]\n\nMissing any required attribute raises ``InvalidNodeSchemaError``.\n"""\n\nfrom __future__ import annotations\n\nimport json\nimport re\nfrom dataclasses import dataclass, field\nfrom pathlib import Path\nfrom typing import TYPE_CHECKING, Any\n\nif TYPE_CHECKING:\n    from .adapter import AdapterPacket\n\nfrom lxml import etree, html  # type: ignore[import-untyped]\n\nfrom .exceptions import InvalidNodeSchemaError\n\n# ---------------------------------------------------------------------------\n# Internal constants\n# ---------------------------------------------------------------------------\n\n_REQUIRED_ARTICLE_ATTRS: tuple[str, ...] = (\n    "data-node-id",\n    "data-node-type",\n)\n\n# Matches <a> elements that carry relationship metadata\n_REL_ANCHOR_RE = re.compile(\n    r'<a\s[^>]*data-relationship-type=["\'][^"\']*["\'][^>]*>',\n    re.IGNORECASE | re.DOTALL,\n)\n\n\n# ---------------------------------------------------------------------------\n# Public dataclasses\n# ---------------------------------------------------------------------------\n\n\n@dataclass(frozen=True, slots=True)\nclass RelationshipAnchor:\n    """\n    Represents a typed relationship link from one Neural Node to another.\n\n    Attributes\n    ----------\n    href : str\n        Relative or absolute path/URL of the linked node document.\n    relationship_type : str\n        Semantic relationship label (e.g. 'depends-on', 'extends',\n        'contradicts', 'authored-by').\n    relationship_weight : float\n        The relevance weight of this specific connection edge in [0.0, 1.0].\n    label : str\n        Human-readable display text of the anchor.\n    """\n\n    href: str\n    relationship_type: str\n    relationship_weight: float\n    label: str\n\n\n@dataclass(slots=True)\nclass NeuralNode:\n    """\n    A fully parsed and validated HTML Neural Node.\n\n    Fields\n    ------\n    node_id : str\n        Unique stable identifier for this node (from ``data-node-id``).\n    node_type : str\n        Categorical type tag (from ``data-node-type``).\n    governance_status : str\n        Governance status string (e.g. 'context-only', 'doctrine').\n    execution_authority : float\n        Governance authority level in [0.0, 1.0].\n    user_approval_strength : float\n        Approval strength in [0.0, 1.0].\n    weight_relevance : float\n        Initial semantic relevance weight in [0.0, 1.0].\n    weight_confidence : float\n        Confidence weight in [0.0, 1.0].\n    weight_behavior_influence : float\n        Behavior influence weight in [0.0, 1.0].\n    weight_governance_importance : float\n        Governance weight in [0.0, 1.0].\n    memory_authority : float\n        Memory authority level in [0.0, 1.0].\n    provider_authority : float\n        Provider authority level in [0.0, 1.0].\n    wallet_authority : float\n        Wallet authority level in [0.0, 1.0].\n    semantic_memory : str\n        Full text content of the node's semantic memory section.\n    relationships : list[RelationshipAnchor]\n        All typed outbound relationship links declared in the document.\n    state_data : dict[str, Any]\n        Parsed JSON state block.\n    """\n\n    node_id: str\n    node_type: str\n    governance_status: str\n    execution_authority: float\n    user_approval_strength: float\n    weight_relevance: float\n    weight_confidence: float\n    weight_behavior_influence: float\n    weight_governance_importance: float\n    memory_authority: float\n    provider_authority: float\n    wallet_authority: float\n    semantic_memory: str\n    relationships: list[RelationshipAnchor]\n    state_data: dict[str, Any]\n    schema_version: str | None = None\n    chaincode_id: str | None = None\n    content_hash: str | None = None\n    source: str | None = None\n    trust_tier: str | None = None\n    node_provenance: dict[str, Any] | None = None\n    memory_type: str | None = None\n    executable_code: str | None = None\n    adapter: "AdapterPacket | None" = None\n\n\n\n# ---------------------------------------------------------------------------\n# Internal helpers\n# ---------------------------------------------------------------------------\n\n\ndef _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:\n    """Return *value* clamped to [lo, hi]."""\n    return max(lo, min(hi, value))\n\n\ndef _parse_authority(raw: str, path: str | None) -> float:\n    """\n    Convert *raw* string to a float and clamp to [0.0, 1.0].\n\n    Raises\n    ------\n    InvalidNodeSchemaError\n        If *raw* cannot be interpreted as a number.\n    """\n    try:\n        return _clamp(float(raw))\n    except (ValueError, TypeError) as exc:\n        raise InvalidNodeSchemaError(\n            f"data-execution-authority value {raw!r} is not a valid float.",\n            path=path,\n            missing_attributes=[],\n            offending_html=f'data-execution-authority="{raw}"',\n        ) from exc\n\n\ndef _validate_article(\n    article: etree._Element,  # noqa: SLF001  (lxml internal)\n    path: str | None,\n) -> None:\n    """\n    Assert that *article* carries all required attributes.\n\n    Raises\n    ------\n    InvalidNodeSchemaError\n        If any required attribute is absent or empty.\n    """\n    missing: list[str] = []\n    for attr in _REQUIRED_ARTICLE_ATTRS:\n        val = article.get(attr, "").strip()\n        if not val:\n            missing.append(attr)\n\n    if missing:\n        snippet = html.tostring(article, encoding="unicode")[:512]\n        raise InvalidNodeSchemaError(\n            f"Neural Node is missing required attributes: {missing}.",\n            path=path,\n            missing_attributes=missing,\n            offending_html=snippet,\n        )\n\n\ndef _extract_semantic_memory(article: etree._Element) -> str:\n    """\n    Return the text content of the element whose ``id`` is\n    ``'semantic-memory'``, or the entire article text as fallback.\n    """\n    candidates = article.xpath('.//*[@id="semantic-memory"]')\n    if candidates:\n        # itertext() flattens all descendant text nodes\n        return " ".join(candidates[0].itertext()).strip()\n    # Fallback: whole article minus script content\n    parts: list[str] = []\n    for elem in article.iter():\n        if elem.tag in ("script", "style"):\n            continue\n        if elem.text:\n            parts.append(elem.text.strip())\n        if elem.tail:\n            parts.append(elem.tail.strip())\n    return " ".join(filter(None, parts))\n\n\ndef _extract_relationships(article: etree._Element) -> list[RelationshipAnchor]:\n    """\n    Collect every ``<a data-relationship-type="...">`` anchor inside\n    *article* and return them as :class:`RelationshipAnchor` objects.\n    """\n    anchors: list[RelationshipAnchor] = []\n    for a in article.xpath('.//a[@data-relationship-type]'):\n        href: str = (a.get("href") or "").strip()\n        rel_type: str = (a.get("data-relationship-type") or "unknown").strip()\n        raw_weight = a.get("data-relationship-weight", "0.5").strip()\n        try:\n            rel_weight = _clamp(float(raw_weight))\n        except (ValueError, TypeError):\n            rel_weight = 0.5\n        label: str = " ".join(a.itertext()).strip()\n        anchors.append(RelationshipAnchor(\n            href=href,\n            relationship_type=rel_type,\n            relationship_weight=rel_weight,\n            label=label\n        ))\n    return anchors\n\n\ndef _extract_json_block(\n    article: etree._Element,\n    script_id: str,\n    path: str | None,\n) -> dict[str, Any]:\n    scripts = article.xpath(\n        f'.//script[@type="application/json" and @id="{script_id}"]'\n    )\n    if not scripts:\n        return {}\n\n    raw_text: str = (scripts[0].text or "").strip()\n    if not raw_text:\n        return {}\n\n    try:\n        parsed = json.loads(raw_text)\n    except json.JSONDecodeError as exc:\n        raise InvalidNodeSchemaError(\n            f"{script_id} JSON block is malformed: {exc}",\n            path=path,\n            missing_attributes=[],\n            offending_html=raw_text[:512],\n        ) from exc\n\n    if not isinstance(parsed, dict):\n        raise InvalidNodeSchemaError(\n            f"{script_id} JSON block must be a JSON object (dict), "\n            f"got {type(parsed).__name__!r}.",\n            path=path,\n            missing_attributes=[],\n            offending_html=raw_text[:512],\n        )\n\n    return parsed  # type: ignore[return-value]\n\n\ndef _extract_state_data(\n    article: etree._Element,\n    path: str | None,\n) -> dict[str, Any]:\n    return _extract_json_block(article, "node-state", path)\n\n\ndef _extract_executable_code(article: etree._Element) -> str | None:\n    """\n    Find ``<script type="application/x-python" id="node-executable">`` inside\n    *article* and return its text content. Returns None if absent.\n    """\n    scripts = article.xpath(\n        './/script[@type="application/x-python" and @id="node-executable"]'\n    )\n    if not scripts:\n        return None\n\n    raw_text: str = (scripts[0].text or "").strip()\n    return raw_text if raw_text else None\n\n\ndef _parse_tree(\n    tree: etree._ElementTree,  # noqa: SLF001\n    path: str | None,\n) -> NeuralNode:\n    """\n    Core parsing logic shared by :func:`parse_file` and :func:`parse_html`.\n\n    Parameters\n    ----------\n    tree :\n        An lxml element tree rooted at the document ``<html>`` element.\n    path :\n        Filesystem path used for error messages (``None`` for in-memory HTML).\n\n    Returns\n    -------\n    NeuralNode\n        Fully populated and validated node.\n    """\n    # Locate the <article> wrapper element\n    articles = tree.xpath('//article[@data-node-id]')\n    if not articles:\n        # Try without the attribute filter (schema violation detected below)\n        articles = tree.xpath('//article')\n\n    if not articles:\n        raise InvalidNodeSchemaError(\n            "No <article> element found in the document.",\n            path=path,\n            missing_attributes=list(_REQUIRED_ARTICLE_ATTRS),\n            offending_html=None,\n        )\n\n    article: etree._Element = articles[0]\n\n    # Schema validation — raises if any required attribute is missing\n    _validate_article(article, path)\n\n    node_id: str = article.get("data-node-id", "").strip()\n    node_type: str = article.get("data-node-type", "").strip()\n    governance_status: str = article.get("data-governance-status", "context-only").strip()\n    \n    execution_authority_str = article.get("data-execution-authority", "").strip()\n    if not execution_authority_str:\n        snippet = html.tostring(article, encoding="unicode")[:512]\n        raise InvalidNodeSchemaError(\n            "Neural Node is missing data-execution-authority attribute.",\n            path=path,\n            missing_attributes=["data-execution-authority"],\n            offending_html=snippet,\n        )\n            \n    execution_authority: float = _parse_authority(execution_authority_str, path)\n    \n    raw_approval = article.get("data-user-approval-strength", "0.00").strip()\n    try:\n        user_approval_strength = _clamp(float(raw_approval))\n    except (ValueError, TypeError):\n        user_approval_strength = 0.0\n\n    # Weights\n    raw_relevance = article.get("data-weight-relevance", "0.5").strip()\n    try:\n        weight_relevance = _clamp(float(raw_relevance))\n    except (ValueError, TypeError):\n        weight_relevance = 0.5\n\n    raw_confidence = article.get("data-weight-confidence", "0.5").strip()\n    try:\n        weight_confidence = _clamp(float(raw_confidence))\n    except (ValueError, TypeError):\n        weight_confidence = 0.5\n\n    raw_behavior = article.get("data-weight-behavior-influence", "0.0").strip()\n    try:\n        weight_behavior_influence = _clamp(float(raw_behavior))\n    except (ValueError, TypeError):\n        weight_behavior_influence = 0.0\n\n    raw_gov_importance = article.get("data-weight-governance-importance", "0.0").strip()\n    try:\n        weight_governance_importance = _clamp(float(raw_gov_importance))\n    except (ValueError, TypeError):\n        weight_governance_importance = 0.0\n\n    # Authorities\n    raw_mem_auth = article.get("data-memory-authority", "0.00").strip()\n    try:\n        memory_authority = _clamp(float(raw_mem_auth))\n    except (ValueError, TypeError):\n        memory_authority = 0.0\n\n    raw_prov_auth = article.get("data-provider-authority", "0.00").strip()\n    try:\n        provider_authority = _clamp(float(raw_prov_auth))\n    except (ValueError, TypeError):\n        provider_authority = 0.0\n\n    raw_wallet_auth = article.get("data-wallet-authority", "0.00").strip()\n    try:\n        wallet_authority = _clamp(float(raw_wallet_auth))\n    except (ValueError, TypeError):\n        wallet_authority = 0.0\n\n    schema_version: str = article.get("data-schema-version", "neural-node/v1").strip()\n    chaincode_id: str = article.get("data-chaincode-id", "").strip() or None\n    content_hash: str = article.get("data-content-hash", "").strip() or None\n    source: str = article.get("data-source", "").strip() or None\n    trust_tier: str = article.get("data-trust-tier", "").strip() or None\n    \n    semantic_memory: str = _extract_semantic_memory(article)\n    relationships: list[RelationshipAnchor] = _extract_relationships(article)\n    state_data: dict[str, Any] = _extract_state_data(article, path)\n    \n    adapter_data = _extract_json_block(article, "adapter-packet", path)\n    if not adapter_data:\n        adapter_data = state_data.get("adapter", {})\n        \n    node_provenance = _extract_json_block(article, "node-provenance", path)\n    if not node_provenance:\n        node_provenance = None\n        \n    executable_code: str | None = _extract_executable_code(article)\n\n    # Extract adapter packet from state_data if present\n    adapter = None\n    if adapter_data:\n        from .adapter import AdapterPacket\n        adapter = AdapterPacket.from_dict(adapter_data)\n\n    # Extract memory_type from data-memory-type attribute or fallback to plaintext header\n    memory_type = article.get("data-memory-type", "").strip() or None\n    if not memory_type:\n        m_type = re.search(r"(?i)Type:\s*([^\n<]+)", semantic_memory[:400])\n        if m_type:\n            memory_type = m_type.group(1).strip()\n\n    return NeuralNode(\n        node_id=node_id,\n        node_type=node_type,\n        governance_status=governance_status,\n        execution_authority=execution_authority,\n        user_approval_strength=user_approval_strength,\n        weight_relevance=weight_relevance,\n        weight_confidence=weight_confidence,\n        weight_behavior_influence=weight_behavior_influence,\n        weight_governance_importance=weight_governance_importance,\n        memory_authority=memory_authority,\n        provider_authority=provider_authority,\n        wallet_authority=wallet_authority,\n        semantic_memory=semantic_memory,\n        relationships=relationships,\n        state_data=state_data,\n        schema_version=schema_version,\n        chaincode_id=chaincode_id,\n        content_hash=content_hash,\n        source=source,\n        trust_tier=trust_tier,\n        node_provenance=node_provenance,\n        memory_type=memory_type,\n        executable_code=executable_code,\n        adapter=adapter,\n    )\n\n\n\n# ---------------------------------------------------------------------------\n# Public API\n# ---------------------------------------------------------------------------\n\n\ndef parse_file(path: str) -> NeuralNode:\n    """\n    Parse an HTML Neural Node from a file on disk.\n\n    The file is opened **read-only** using ``pathlib.Path.read_text()``.\n    The parser never writes to disk.\n\n    Parameters\n    ----------\n    path : str\n        Absolute or relative filesystem path to the ``.html`` node file.\n\n    Returns\n    -------\n    NeuralNode\n        Fully parsed and validated Neural Node.\n\n    Raises\n... (file truncated, showing first 500 lines) ...
```

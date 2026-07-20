"""
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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapter import AdapterPacket

from lxml import etree, html  # type: ignore[import-untyped]

from .exceptions import InvalidNodeSchemaError

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_REQUIRED_ARTICLE_ATTRS: tuple[str, ...] = (
    "data-node-id",
    "data-node-type",
)

# Matches <a> elements that carry relationship metadata
_REL_ANCHOR_RE = re.compile(
    r'<a\s[^>]*data-relationship-type=["\'][^"\']*["\'][^>]*>',
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationshipAnchor:
    """
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
    """

    href: str
    relationship_type: str
    relationship_weight: float
    label: str


@dataclass(slots=True)
class NeuralNode:
    """
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
    """

    node_id: str
    node_type: str
    governance_status: str
    execution_authority: float
    user_approval_strength: float
    weight_relevance: float
    weight_confidence: float
    weight_behavior_influence: float
    weight_governance_importance: float
    memory_authority: float
    provider_authority: float
    wallet_authority: float
    semantic_memory: str
    relationships: list[RelationshipAnchor]
    state_data: dict[str, Any]
    schema_version: str | None = None
    chaincode_id: str | None = None
    content_hash: str | None = None
    source: str | None = None
    trust_tier: str | None = None
    node_provenance: dict[str, Any] | None = None
    memory_type: str | None = None
    executable_code: str | None = None
    adapter: "AdapterPacket | None" = None
    lora_adapter: str | None = None



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Return *value* clamped to [lo, hi]."""
    return max(lo, min(hi, value))


def _parse_authority(raw: str, path: str | None) -> float:
    """
    Convert *raw* string to a float and clamp to [0.0, 1.0].

    Raises
    ------
    InvalidNodeSchemaError
        If *raw* cannot be interpreted as a number.
    """
    try:
        return _clamp(float(raw))
    except (ValueError, TypeError) as exc:
        raise InvalidNodeSchemaError(
            f"data-execution-authority value {raw!r} is not a valid float.",
            path=path,
            missing_attributes=[],
            offending_html=f'data-execution-authority="{raw}"',
        ) from exc


def _validate_article(
    article: etree._Element,  # noqa: SLF001  (lxml internal)
    path: str | None,
) -> None:
    """
    Assert that *article* carries all required attributes.

    Raises
    ------
    InvalidNodeSchemaError
        If any required attribute is absent or empty.
    """
    missing: list[str] = []
    for attr in _REQUIRED_ARTICLE_ATTRS:
        val = article.get(attr, "").strip()
        if not val:
            missing.append(attr)

    if missing:
        snippet = html.tostring(article, encoding="unicode")[:512]
        raise InvalidNodeSchemaError(
            f"Neural Node is missing required attributes: {missing}.",
            path=path,
            missing_attributes=missing,
            offending_html=snippet,
        )


def _extract_semantic_memory(article: etree._Element) -> str:
    """
    Return the text content of the element whose ``id`` is
    ``'semantic-memory'``, or the entire article text as fallback.
    """
    candidates = article.xpath('.//*[@id="semantic-memory"]')
    if candidates:
        # itertext() flattens all descendant text nodes
        return " ".join(candidates[0].itertext()).strip()
    # Fallback: whole article minus script content
    parts: list[str] = []
    for elem in article.iter():
        if elem.tag in ("script", "style"):
            continue
        if elem.text:
            parts.append(elem.text.strip())
        if elem.tail:
            parts.append(elem.tail.strip())
    return " ".join(filter(None, parts))


def _extract_relationships(article: etree._Element) -> list[RelationshipAnchor]:
    """
    Collect every ``<a data-relationship-type="...">`` anchor inside
    *article* and return them as :class:`RelationshipAnchor` objects.
    """
    anchors: list[RelationshipAnchor] = []
    for a in article.xpath('.//a[@data-relationship-type]'):
        href: str = (a.get("href") or "").strip()
        rel_type: str = (a.get("data-relationship-type") or "unknown").strip()
        raw_weight = a.get("data-relationship-weight", "0.5").strip()
        try:
            rel_weight = _clamp(float(raw_weight))
        except (ValueError, TypeError):
            rel_weight = 0.5
        label: str = " ".join(a.itertext()).strip()
        anchors.append(RelationshipAnchor(
            href=href,
            relationship_type=rel_type,
            relationship_weight=rel_weight,
            label=label
        ))
    return anchors


def _extract_json_block(
    article: etree._Element,
    script_id: str,
    path: str | None,
) -> dict[str, Any]:
    scripts = article.xpath(
        f'.//script[@type="application/json" and @id="{script_id}"]'
    )
    if not scripts:
        return {}

    raw_text: str = (scripts[0].text or "").strip()
    if not raw_text:
        return {}

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidNodeSchemaError(
            f"{script_id} JSON block is malformed: {exc}",
            path=path,
            missing_attributes=[],
            offending_html=raw_text[:512],
        ) from exc

    if not isinstance(parsed, dict):
        raise InvalidNodeSchemaError(
            f"{script_id} JSON block must be a JSON object (dict), "
            f"got {type(parsed).__name__!r}.",
            path=path,
            missing_attributes=[],
            offending_html=raw_text[:512],
        )

    return parsed  # type: ignore[return-value]


def _extract_state_data(
    article: etree._Element,
    path: str | None,
) -> dict[str, Any]:
    return _extract_json_block(article, "node-state", path)


def _extract_executable_code(article: etree._Element) -> str | None:
    """
    Find ``<script type="application/x-python" id="node-executable">`` inside
    *article* and return its text content. Returns None if absent.
    """
    scripts = article.xpath(
        './/script[@type="application/x-python" and @id="node-executable"]'
    )
    if not scripts:
        return None

    raw_text: str = (scripts[0].text or "").strip()
    return raw_text if raw_text else None


def _parse_tree(
    tree: etree._ElementTree,  # noqa: SLF001
    path: str | None,
) -> NeuralNode:
    """
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
    """
    # Locate the <article> wrapper element
    articles = tree.xpath('//article[@data-node-id]')
    if not articles:
        # Try without the attribute filter (schema violation detected below)
        articles = tree.xpath('//article')

    if not articles:
        raise InvalidNodeSchemaError(
            "No <article> element found in the document.",
            path=path,
            missing_attributes=list(_REQUIRED_ARTICLE_ATTRS),
            offending_html=None,
        )

    article: etree._Element = articles[0]

    # Schema validation — raises if any required attribute is missing
    _validate_article(article, path)

    node_id: str = article.get("data-node-id", "").strip()
    node_type: str = article.get("data-node-type", "").strip()
    governance_status: str = article.get("data-governance-status", "context-only").strip()
    
    execution_authority_str = article.get("data-execution-authority", "").strip()
    if not execution_authority_str:
        snippet = html.tostring(article, encoding="unicode")[:512]
        raise InvalidNodeSchemaError(
            "Neural Node is missing data-execution-authority attribute.",
            path=path,
            missing_attributes=["data-execution-authority"],
            offending_html=snippet,
        )
            
    execution_authority: float = _parse_authority(execution_authority_str, path)
    
    raw_approval = article.get("data-user-approval-strength", "0.00").strip()
    try:
        user_approval_strength = _clamp(float(raw_approval))
    except (ValueError, TypeError):
        user_approval_strength = 0.0

    # Weights
    raw_relevance = article.get("data-weight-relevance", "0.5").strip()
    try:
        weight_relevance = _clamp(float(raw_relevance))
    except (ValueError, TypeError):
        weight_relevance = 0.5

    raw_confidence = article.get("data-weight-confidence", "0.5").strip()
    try:
        weight_confidence = _clamp(float(raw_confidence))
    except (ValueError, TypeError):
        weight_confidence = 0.5

    raw_behavior = article.get("data-weight-behavior-influence", "0.0").strip()
    try:
        weight_behavior_influence = _clamp(float(raw_behavior))
    except (ValueError, TypeError):
        weight_behavior_influence = 0.0

    raw_gov_importance = article.get("data-weight-governance-importance", "0.0").strip()
    try:
        weight_governance_importance = _clamp(float(raw_gov_importance))
    except (ValueError, TypeError):
        weight_governance_importance = 0.0

    # Authorities
    raw_mem_auth = article.get("data-memory-authority", "0.00").strip()
    try:
        memory_authority = _clamp(float(raw_mem_auth))
    except (ValueError, TypeError):
        memory_authority = 0.0

    raw_prov_auth = article.get("data-provider-authority", "0.00").strip()
    try:
        provider_authority = _clamp(float(raw_prov_auth))
    except (ValueError, TypeError):
        provider_authority = 0.0

    raw_wallet_auth = article.get("data-wallet-authority", "0.00").strip()
    try:
        wallet_authority = _clamp(float(raw_wallet_auth))
    except (ValueError, TypeError):
        wallet_authority = 0.0

    schema_version: str = article.get("data-schema-version", "neural-node/v1").strip()
    chaincode_id: str = article.get("data-chaincode-id", "").strip() or None
    content_hash: str = article.get("data-content-hash", "").strip() or None
    source: str = article.get("data-source", "").strip() or None
    trust_tier: str = article.get("data-trust-tier", "").strip() or None
    lora_adapter: str | None = article.get("data-lora-adapter", "").strip() or None
    
    semantic_memory: str = _extract_semantic_memory(article)
    relationships: list[RelationshipAnchor] = _extract_relationships(article)
    state_data: dict[str, Any] = _extract_state_data(article, path)
    
    adapter_data = _extract_json_block(article, "adapter-packet", path)
    if not adapter_data:
        adapter_data = state_data.get("adapter", {})
        
    node_provenance = _extract_json_block(article, "node-provenance", path)
    if not node_provenance:
        node_provenance = None
        
    executable_code: str | None = _extract_executable_code(article)

    # Extract adapter packet from state_data if present
    adapter = None
    if adapter_data:
        from .adapter import AdapterPacket
        adapter = AdapterPacket.from_dict(adapter_data)

    # Extract memory_type from data-memory-type attribute or fallback to plaintext header
    memory_type = article.get("data-memory-type", "").strip() or None
    if not memory_type:
        m_type = re.search(r"(?i)Type:\s*([^\n<]+)", semantic_memory[:400])
        if m_type:
            memory_type = m_type.group(1).strip()

    return NeuralNode(
        node_id=node_id,
        node_type=node_type,
        governance_status=governance_status,
        execution_authority=execution_authority,
        user_approval_strength=user_approval_strength,
        weight_relevance=weight_relevance,
        weight_confidence=weight_confidence,
        weight_behavior_influence=weight_behavior_influence,
        weight_governance_importance=weight_governance_importance,
        memory_authority=memory_authority,
        provider_authority=provider_authority,
        wallet_authority=wallet_authority,
        semantic_memory=semantic_memory,
        relationships=relationships,
        state_data=state_data,
        schema_version=schema_version,
        chaincode_id=chaincode_id,
        content_hash=content_hash,
        source=source,
        trust_tier=trust_tier,
        node_provenance=node_provenance,
        memory_type=memory_type,
        executable_code=executable_code,
        adapter=adapter,
        lora_adapter=lora_adapter,
    )



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_file(path: str) -> NeuralNode:
    """
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
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Neural Node file not found: {path!r}")

    raw_html: str = source.read_text(encoding="utf-8")
    tree: etree._ElementTree = html.fromstring(raw_html).getroottree()
    return _parse_tree(tree, str(source))


def parse_html(html_str: str) -> NeuralNode:
    """
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
    """
    if not html_str or not html_str.strip():
        raise InvalidNodeSchemaError(
            "Cannot parse an empty HTML string.",
            path=None,
            missing_attributes=list(_REQUIRED_ARTICLE_ATTRS),
            offending_html=None,
        )
    tree: etree._ElementTree = html.fromstring(html_str).getroottree()
    return _parse_tree(tree, path=None)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "NeuralNode",
    "RelationshipAnchor",
    "parse_file",
    "parse_html",
]

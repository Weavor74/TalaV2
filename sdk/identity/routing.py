"""
sdk/identity/routing.py — Tala Cognitive Engine Room: Connection Routing Adapter
=================================================================================
Parses peer identity nodes and derives formatting / communication boundaries
so the engine room knows exactly how to behave when speaking to a given peer.

Design constraints
------------------
* ``_parse_peer_identity()`` performs ALL parsing locally using lxml — it NEVER
  makes an outbound LLM or network call.  This is a hard architectural rule:
  identity negotiation must be deterministic and offline-capable.
* The parser is intentionally lenient: missing fields fall back to safe
  defaults rather than raising, because peer nodes may be authored by
  third-party agents that do not fully comply with the Tala schema.
* ``_map_sender_to_config()`` maps a :class:`PeerIdentity` to a
  :class:`FormattingConfig` using a simple rule table rather than ML
  inference — again, to keep routing deterministic and auditable.

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PeerIdentity:
    """
    Normalised identity record extracted from a peer agent's whoami node.

    Attributes
    ----------
    agent_id : str
        Unique node identifier (from ``data-node-id``).
    clearance_level : int
        Integer clearance level in [0, 10].  Defaults to 0 (untrusted) if
        the peer's node does not declare one.
    allowed_tools : list[str]
        The MCP tool endpoints the peer declares it can invoke.  Used for
        capability-based trust decisions.
    sender_group : str
        The *highest* sender group the peer belongs to, used for routing.
        Possible values: ``"admin"``, ``"verified_partner"``,
        ``"trusted_user"``, ``"context_only_agent"``, ``"unknown"``.
    style_preferences : dict[str, Any]
        Raw formatting preferences dict from the peer's state block.
        Empty dict if not declared.
    raw_state : dict[str, Any]
        Full unparsed state_data dict for callers that need raw access.
    """

    agent_id: str
    clearance_level: int
    allowed_tools: list[str]
    sender_group: str
    style_preferences: dict[str, Any]
    raw_state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class FormattingConfig:
    """
    Derived formatting / styling boundaries for communicating with a peer.

    Attributes
    ----------
    max_response_length : int
        Maximum number of characters allowed in a single response to this peer.
    allowed_formats : list[str]
        Output formats Tala may use when responding to this peer.
        Always a subset of ``["markdown", "plain_text", "html_fragment"]``.
    tone : str
        Communication tone register.  One of:
        ``"formal"``, ``"warm_professional"``, ``"concise"``, ``"minimal"``.
    language : str
        BCP-47 language tag, e.g. ``"en-US"``.
    """

    max_response_length: int
    allowed_formats: list[str]
    tone: str
    language: str


# ---------------------------------------------------------------------------
# Clearance-to-group precedence table
# ---------------------------------------------------------------------------

# Ordered from highest to lowest precedence.  When a peer declares multiple
# sender groups, we pick the first match in this list.
_GROUP_PRECEDENCE: list[str] = [
    "admin",
    "verified_partner",
    "trusted_user",
    "context_only_agent",
]

# Default formatting configs keyed by sender_group
_GROUP_FORMATTING: dict[str, FormattingConfig] = {
    "admin": FormattingConfig(
        max_response_length=8192,
        allowed_formats=["markdown", "plain_text", "html_fragment"],
        tone="warm_professional",
        language="en-US",
    ),
    "verified_partner": FormattingConfig(
        max_response_length=4096,
        allowed_formats=["markdown", "plain_text"],
        tone="warm_professional",
        language="en-US",
    ),
    "trusted_user": FormattingConfig(
        max_response_length=2048,
        allowed_formats=["markdown", "plain_text"],
        tone="concise",
        language="en-US",
    ),
    "context_only_agent": FormattingConfig(
        max_response_length=1024,
        allowed_formats=["plain_text"],
        tone="minimal",
        language="en-US",
    ),
    "unknown": FormattingConfig(
        max_response_length=512,
        allowed_formats=["plain_text"],
        tone="formal",
        language="en-US",
    ),
}


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------


def _parse_peer_identity(whoami_html: str) -> PeerIdentity:
    """
    Parse a peer agent's whoami HTML Neural Node into a :class:`PeerIdentity`.

    This function runs **entirely locally** — no outbound calls, no LLM
    inference.  It uses lxml to extract the ``<article>`` attributes and
    the ``<script type="application/json" id="node-state">`` block.

    Parameters
    ----------
    whoami_html : str
        Raw HTML content of the peer's whoami node.

    Returns
    -------
    PeerIdentity
        Parsed and normalised identity record.  Missing fields fall back to
        safe defaults rather than raising.

    Notes
    -----
    If the peer's HTML is malformed or the state block is absent, the
    function returns a :class:`PeerIdentity` with ``clearance_level=0``,
    ``sender_group="unknown"``, and empty tool list — the most restrictive
    possible interpretation.
    """
    # Deferred import — lxml is only required when this function is called
    try:
        from lxml import html as lxml_html  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "lxml is required for _parse_peer_identity().  "
            "Install it with: pip install lxml"
        ) from exc

    if not whoami_html or not whoami_html.strip():
        logger.warning("parse_peer_identity received empty HTML; returning unknown peer.")
        return _unknown_peer("empty-html-input")

    # ----------------------------------------------------------------
    # Parse HTML tree
    # ----------------------------------------------------------------
    try:
        tree = lxml_html.fromstring(whoami_html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lxml parse error for peer whoami: %s", exc)
        return _unknown_peer("lxml-parse-error")

    # ----------------------------------------------------------------
    # Locate <article> element
    # ----------------------------------------------------------------
    articles = tree.xpath('//article[@data-node-id]')
    if not articles:
        articles = tree.xpath('//article')
    if not articles:
        logger.warning("No <article> found in peer whoami node.")
        return _unknown_peer("no-article-element")

    article = articles[0]

    agent_id: str = (article.get("data-node-id") or "").strip() or "unknown-peer"

    # ----------------------------------------------------------------
    # Parse state JSON block (SECURITY: json.loads only, never eval)
    # ----------------------------------------------------------------
    state: dict[str, Any] = {}
    state_scripts = article.xpath(
        './/script[@type="application/json" and @id="node-state"]'
    )
    if state_scripts:
        raw_json = (state_scripts[0].text or "").strip()
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    state = parsed
                else:
                    logger.warning(
                        "Peer node-state JSON is not a dict (got %s); ignoring.",
                        type(parsed).__name__,
                    )
            except json.JSONDecodeError as exc:
                logger.warning("Peer node-state JSON malformed: %s", exc)

    # ----------------------------------------------------------------
    # Extract fields with safe defaults
    # ----------------------------------------------------------------
    clearance_level: int = _safe_int(state.get("clearance_level", 0), fallback=0)
    clearance_level = max(0, min(10, clearance_level))  # clamp [0, 10]

    allowed_tools: list[str] = [
        str(t) for t in state.get("allowed_mcp_tool_endpoints", [])
        if isinstance(t, str)
    ]

    # Determine the highest-precedence sender group this peer belongs to
    peer_groups: list[str] = [
        str(g) for g in state.get("allowed_sender_groups", [])
        if isinstance(g, str)
    ]
    sender_group: str = _resolve_sender_group(peer_groups)

    style_preferences: dict[str, Any] = dict(
        state.get("formatting_preferences", {})
    )

    logger.debug(
        "Parsed peer identity: agent_id=%r clearance=%d group=%r tools=%d",
        agent_id, clearance_level, sender_group, len(allowed_tools),
    )

    return PeerIdentity(
        agent_id=agent_id,
        clearance_level=clearance_level,
        allowed_tools=allowed_tools,
        sender_group=sender_group,
        style_preferences=style_preferences,
        raw_state=state,
    )


# ---------------------------------------------------------------------------
# Formatter mapper
# ---------------------------------------------------------------------------


def _map_sender_to_config(peer: PeerIdentity) -> FormattingConfig:
    """
    Derive a :class:`FormattingConfig` from a parsed :class:`PeerIdentity`.

    The mapping is rule-based:

    1. Look up the base config for the peer's ``sender_group``.
    2. If the peer declares a ``max_response_length`` in their style
       preferences, use the *minimum* of that and the base limit (never
       allow the peer to demand a longer response than the group limit).
    3. If the peer declares a ``language`` preference, honour it — language
       selection is a cosmetic choice that cannot escalate authority.

    Parameters
    ----------
    peer : PeerIdentity
        Parsed peer identity record.

    Returns
    -------
    FormattingConfig
        Immutable formatting boundaries for this peer.
    """
    base: FormattingConfig = _GROUP_FORMATTING.get(
        peer.sender_group, _GROUP_FORMATTING["unknown"]
    )

    # Apply peer-declared overrides that cannot increase authority
    max_len = base.max_response_length
    if "max_response_length" in peer.style_preferences:
        peer_max = _safe_int(peer.style_preferences["max_response_length"], fallback=max_len)
        max_len = min(max_len, max(64, peer_max))  # never below 64 chars

    language = base.language
    peer_lang = peer.style_preferences.get("preferred_language", "")
    if peer_lang and isinstance(peer_lang, str) and 2 <= len(peer_lang) <= 10:
        language = peer_lang.strip()

    logger.debug(
        "FormattingConfig for %r: tone=%r max_len=%d lang=%r",
        peer.agent_id, base.tone, max_len, language,
    )

    return FormattingConfig(
        max_response_length=max_len,
        allowed_formats=list(base.allowed_formats),
        tone=base.tone,
        language=language,
    )


# ---------------------------------------------------------------------------
# ConnectionRouter facade
# ---------------------------------------------------------------------------


class ConnectionRouter:
    """
    High-level facade that combines peer identity parsing and config mapping.

    Typical usage::

        router = ConnectionRouter()
        peer = router.identify(peer_whoami_html)
        config = router.configure(peer)

    All methods are synchronous and make no outbound calls.
    """

    def identify(self, whoami_html: str) -> PeerIdentity:
        """Parse a peer's whoami HTML and return their :class:`PeerIdentity`."""
        return _parse_peer_identity(whoami_html)

    def configure(self, peer: PeerIdentity) -> FormattingConfig:
        """Derive :class:`FormattingConfig` from a :class:`PeerIdentity`."""
        return _map_sender_to_config(peer)

    def identify_and_configure(
        self, whoami_html: str
    ) -> tuple[PeerIdentity, FormattingConfig]:
        """
        Convenience: parse identity and derive config in one call.

        Returns
        -------
        tuple[PeerIdentity, FormattingConfig]
        """
        peer = self.identify(whoami_html)
        config = self.configure(peer)
        return peer, config


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _unknown_peer(reason: str) -> PeerIdentity:
    """Return the most restrictive possible PeerIdentity (safe fallback)."""
    return PeerIdentity(
        agent_id=f"unknown-{reason}",
        clearance_level=0,
        allowed_tools=[],
        sender_group="unknown",
        style_preferences={},
        raw_state={},
    )


def _resolve_sender_group(peer_groups: list[str]) -> str:
    """
    Find the highest-precedence group that the peer belongs to.

    Falls back to ``"unknown"`` if none match.
    """
    for group in _GROUP_PRECEDENCE:
        if group in peer_groups:
            return group
    return "unknown"


def _safe_int(value: Any, fallback: int) -> int:
    """Convert *value* to int; return *fallback* on any error."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "PeerIdentity",
    "FormattingConfig",
    "ConnectionRouter",
    "parse_peer_identity",
    "map_sender_to_config",
]

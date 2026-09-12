"""
sdk/identity/whoami.py — Tala Cognitive Engine Room: WhoamiNode Wrapper
========================================================================
Typed Python wrapper around a parsed whoami Neural Node.  Provides
convenient attribute access over the raw NeuralNode + state_data dict
so that the rest of the engine room never has to manually index into
the JSON state block.

Milestone 4 — Identity & Marketplace
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Locate the canonical whoami.html bundled alongside this module
_WHOAMI_DEFAULT_PATH: Path = Path(__file__).parent / "whoami.html"


@dataclass(slots=True)
class WhoamiNode:
    """
    Typed representation of Tala's parsed identity node.

    This class wraps the raw :class:`~sdk.agent.parser.NeuralNode` and
    exposes strongly-typed accessors for every field declared in the
    ``node-state`` JSON block of ``whoami.html``.

    Attributes
    ----------
    agent_name : str
        Display name of this agent instance.
    agent_version : str
        Semantic version string, e.g. ``"2.0.0"``.
    birthdate : str
        ISO-8601 birthdate string declared on the ``<article>`` element.
    clearance_level : int
        Integer authority level in [0, 10].
    allowed_mcp_tool_endpoints : list[str]
        Allowlisted MCP tool endpoint URIs this agent may invoke.
    allowed_sender_groups : list[str]
        Sender group tags accepted by this agent.
    formatting_preferences : dict[str, Any]
        Raw formatting/styling preferences dict from state block.
    style_governance : dict[str, Any]
        Style capping and governance parameters.
    signature : dict[str, str]
        HMAC signature envelope (algorithm, key_id, value).
    execution_authority : float
        The ``data-execution-authority`` value from the article element,
        always 1.00 for the master identity node.
    """

    agent_name: str
    agent_version: str
    birthdate: str
    clearance_level: int
    allowed_mcp_tool_endpoints: list[str]
    allowed_sender_groups: list[str]
    formatting_preferences: dict[str, Any]
    style_governance: dict[str, Any]
    signature: dict[str, str]
    execution_authority: float = 1.0

    # ----------------------------------------------------------------
    # Factory
    # ----------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "WhoamiNode":
        """
        Parse and return a :class:`WhoamiNode` from the given HTML file.

        Parameters
        ----------
        path :
            Filesystem path to the ``whoami.html`` file.  Defaults to the
            ``whoami.html`` bundled in this package directory.

        Returns
        -------
        WhoamiNode
            Fully populated identity wrapper.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist on disk.
        ImportError
            If the SDK agent parser module is unavailable (dependency issue).
        sdk.agent.exceptions.InvalidNodeSchemaError
            If the HTML document fails Neural Node schema validation.
        """
        # Deferred import to avoid circular dependency at package init time
        from sdk.agent.parser import parse_file  # type: ignore[import]

        resolved = Path(path) if path else _WHOAMI_DEFAULT_PATH
        logger.debug("Loading WhoamiNode from: %s", resolved)

        node = parse_file(str(resolved))
        sd: dict[str, Any] = node.state_data

        return cls(
            agent_name=sd.get("agent_name", "Tala"),
            agent_version=sd.get("agent_version", "unknown"),
            birthdate=sd.get("birthdate", ""),
            clearance_level=int(sd.get("clearance_level", 0)),
            allowed_mcp_tool_endpoints=list(
                sd.get("allowed_mcp_tool_endpoints", [])
            ),
            allowed_sender_groups=list(sd.get("allowed_sender_groups", [])),
            formatting_preferences=dict(sd.get("formatting_preferences", {})),
            style_governance=dict(sd.get("style_governance", {})),
            signature=dict(sd.get("signature", {})),
            execution_authority=node.execution_authority,
        )

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def is_endpoint_allowed(self, endpoint: str) -> bool:
        """Return True if *endpoint* is on the MCP allowlist."""
        return endpoint in self.allowed_mcp_tool_endpoints

    def is_sender_group_allowed(self, group: str) -> bool:
        """Return True if *group* is an accepted sender group."""
        return group in self.allowed_sender_groups

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"WhoamiNode(agent={self.agent_name!r}, "
            f"version={self.agent_version!r}, "
            f"clearance={self.clearance_level}, "
            f"authority={self.execution_authority})"
        )


__all__: list[str] = ["WhoamiNode"]

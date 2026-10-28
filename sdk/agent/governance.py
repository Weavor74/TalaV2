"""
sdk/agent/governance.py
======================
Runtime Governance & Policy Evaluation — Tala Cognitive Engine Room
Milestone 4 | Tasks 4.1, 4.2, 4.3, 4.4

Implements:
  • RuntimeGovernance — Maps directories and tools to specific authority levels.
  • RiskClassifier    — Computes risk scores for target resources and actions.
  • PolicyEvaluator   — Enforces constraints on command lines and neural node scripts.
  • ToolGovernance    — Restricts available tool parameters based on security clearance.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)


class RuntimeGovernance:
    """
    Defines security tiers for execution directories and tool schemas.
    """

    AUTHORITY_TIERS = {
        "read_only": 1,
        "low_risk": 2,
        "privileged": 3,
        "critical": 4,
    }

    @classmethod
    def get_resource_required_tier(cls, path: str) -> str:
        """Get required authority tier for a resource path."""
        norm_path = path.replace(os.path.sep, "/")
        
        # Critical system paths
        if any(token in norm_path for token in ("python/harness", "electron/main.js", "preload.js")):
            return "critical"
            
        # Codebase and SDK files
        if any(token in norm_path for token in ("python/", "sdk/", "blockchain/")):
            return "privileged"
            
        # Database or general data files
        if "data/" in norm_path or "vault/" in norm_path:
            return "low_risk"
            
        return "read_only"


class RiskClassifier:
    """
    Dynamically grades the safety risk of proposed actions.
    """

    @staticmethod
    def classify_action(action_type: str, resource: str) -> str:
        """
        Classifies risk into: 'low', 'medium', 'high', 'critical'.
        """
        # Command execution is always medium/high/critical
        if action_type == "execute_command":
            if any(token in resource for token in ("rm", "del", "format", "shutdown", "net user")):
                return "critical"
            if any(token in resource for token in ("git", "npm", "pip", "pytest")):
                return "high"
            return "medium"

        # File writes
        if action_type == "write_file":
            required_tier = RuntimeGovernance.get_resource_required_tier(resource)
            if required_tier == "critical":
                return "critical"
            if required_tier == "privileged":
                return "high"
            if required_tier == "low_risk":
                return "medium"
            return "low"

        # Tool execution
        if action_type == "execute_tool":
            # desktop_action drives the real mouse and keyboard: one call can
            # click anything on screen, type into whatever holds focus, or send
            # a hotkey to the shell. It reaches further than
            # run_workspace_command, which at least runs inside a workspace, so
            # it belongs no lower.
            #
            # It matters that this is stated here rather than left to the
            # default. Everything not named in these two tuples falls through
            # to "low", so a tool is unclassified and safe-looking by omission
            # -- and the gates read this, not the tool's own description. A
            # capability nobody classified is a capability nobody gated.
            if resource in ("run_workspace_command", "write_workspace_file",
                            "submit_agent_goal", "desktop_action", "desktop_run"):
                return "high"
            if resource in ("send_telegram_message", "verify_and_heal_documentation", "update_neural_node", "open_url"):
                return "medium"
            return "low"

        return "low"


class PolicyEvaluator:
    """
    Validates command execution inputs and Neural Node schema writes.
    """

    @staticmethod
    def evaluate_command(command: str) -> bool:
        """
        Enforce command rules: block dangerous system destruction flags and raw downloads.
        """
        cmd_lower = command.lower()
        
        # Block dangerous flags/phrases
        blocked_phrases = [
            r"rm\s+-rf\s+/",
            r"del\s+/f\s+/q\s+c:",
            r"format\s+c:",
            r"curl\s+.*\bsh\b",
            r"wget\s+.*\bsh\b",
        ]
        
        for phrase in blocked_phrases:
            if re.search(phrase, cmd_lower):
                log.warning("PolicyEvaluator: blocked command match for dangerous pattern: %s", command)
                return False
                
        return True

    @staticmethod
    def evaluate_neural_node_write(file_path: str, data: str) -> bool:
        """
        Tala Design Invariant: Guard against raw Javascript execution tags
        inside HTML Neural Nodes to prevent cross-site/harness scripting.
        """
        if not file_path.endswith(".html"):
            return True
            
        # Parse data to find script tags or javascript attributes
        if "<script" in data.lower() or "javascript:" in data.lower() or "onload=" in data.lower():
            log.warning("PolicyEvaluator: blocked Neural Node write in %s containing JS execution nodes", file_path)
            return False
            
        return True


class ToolGovernance:
    """
    Constrains tool parameter access based on current authority clearances.
    """

    @staticmethod
    def filter_tool_parameters(
        tool_name: str,
        parameters: dict[str, Any],
        clearance_level: str,
    ) -> dict[str, Any]:
        """
        Sanitize or strip arguments if the clearance level is insufficient.
        """
        sanitized = parameters.copy()
        
        # Read-only clearances cannot perform writing operations
        if clearance_level == "read_only":
            if tool_name == "write_file":
                raise PermissionError("clearance_level 'read_only' cannot invoke write_file.")
            if tool_name == "execute_command":
                raise PermissionError("clearance_level 'read_only' cannot invoke execute_command.")
                
        # Low risk clearances can execute commands but cannot pass env vars or run sudo
        if clearance_level in ("read_only", "low_risk"):
            if "env" in sanitized:
                log.warning("ToolGovernance: stripped 'env' arguments from tool %s due to low clearance", tool_name)
                sanitized.pop("env")
            if "user" in sanitized or "sudo" in sanitized:
                log.warning("ToolGovernance: stripped 'sudo' escalation from tool %s due to low clearance", tool_name)
                sanitized.pop("user", None)
                sanitized.pop("sudo", None)

        return sanitized

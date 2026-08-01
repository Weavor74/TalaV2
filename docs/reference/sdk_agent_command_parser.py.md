# File: `command_parser.py`

## Module Description

sdk/agent/command_parser.py
===========================
Governed Command Parser — Tala Cognitive Engine Room
Milestone 9 | Task 9.2

Converts slash command strings into structured GovernedAction objects with
risk classification, routing metadata, and authorization requirements.
Every slash command in the renderer must be parsed through this layer before
being dispatched to ensure deterministic, auditable command handling.

## Classes

### `class GovernedAction`
Structured representation of a parsed slash command.

Every field is deterministic given the same input — this object can be
logged, replayed, and audited.

- **`def to_dict(self)`**
  - Serialize to a plain dict for JSON transport.

### `class ParseResult`
Result of CommandParser.parse().


### `class CommandParser`
Converts a slash command string into a GovernedAction.

Parsing rules:
  • Input must start with '/'.
  • The first token after '/' is the command name.
  • Everything after the command name is the argument string.
  • For /goal and /queue, the full argument becomes goal_text.
  • For /halt and /revert, no arguments are expected.
  • Unknown commands produce ParseResult(success=False).

Risk override:
  The RiskClassifier may upgrade the base risk level based on
  argument content (e.g., a /shell command with 'rm -rf' is critical).

- **`def parse(self, raw_input)`**
  - Parse *raw_input* (a slash command string) into a ParseResult.
  
  Parameters
  ----------
  raw_input:
      The full message text, e.g. '/goal "scan the codebase"'.
  
  Returns
  -------
  ParseResult
      success=True with a GovernedAction, or success=False with
      an error message and suggestion.
- **`def _parse_args(self, command, arg_string)`**
  - Extract structured arguments based on command type.
- **`def _classify_risk(self, command, rpc_method, arg_string, base_risk)`**
  - Override base risk if argument content is dangerous.

## Source Code

```python
"""
sdk/agent/command_parser.py
===========================
Governed Command Parser — Tala Cognitive Engine Room
Milestone 9 | Task 9.2

Converts slash command strings into structured GovernedAction objects with
risk classification, routing metadata, and authorization requirements.
Every slash command in the renderer must be parsed through this layer before
being dispatched to ensure deterministic, auditable command handling.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sdk.agent.governance import RiskClassifier
from sdk.agent.persistence import generate_deterministic_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class GovernedAction:
    """
    Structured representation of a parsed slash command.

    Every field is deterministic given the same input — this object can be
    logged, replayed, and audited.
    """

    action_id: str
    command: str                   # e.g. "goal", "scan", "lint"
    raw_input: str                 # Original slash command string
    args: dict[str, Any]          # Parsed arguments
    risk_level: str                # "low" | "medium" | "high" | "critical"
    requires_confirmation: bool
    routed_to: str                 # RPC method name
    is_batch: bool = False         # True if this queues multiple tasks
    parsed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON transport."""
        return {
            "action_id": self.action_id,
            "command": self.command,
            "raw_input": self.raw_input,
            "args": self.args,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "routed_to": self.routed_to,
            "is_batch": self.is_batch,
            "parsed_at": self.parsed_at.isoformat(),
        }


@dataclass
class ParseResult:
    """Result of CommandParser.parse()."""

    success: bool
    action: GovernedAction | None = None
    error: str | None = None
    suggestion: str | None = None


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------

#: Maps command name → (rpc_method, base_risk, requires_confirmation)
_COMMAND_ROUTES: dict[str, tuple[str, str, bool]] = {
    "goal":   ("task.submit",      "medium", False),
    "queue":  ("task.submit",      "medium", False),
    "scan":   ("repo.scan",        "low",    False),
    "lint":   ("repo.lint",        "low",    False),
    "halt":   ("task.control",     "high",   True),
    "clear":  ("task.clear",       "medium", False),
    "undo":   ("repo.restore",     "high",   True),
    "revert": ("repo.restore",     "high",   True),
    "report": ("task.report",      "low",    False),
    "status": ("task.list",        "low",    False),
    "phase":  ("task.submit",      "medium", False),
    "shell":  ("shell.run",        "high",   True),
    "build":  ("build.diagnose",   "medium", False),
    "fix":    ("build.repair",     "high",   True),
    "test":   ("test.generate",    "medium", False),
    "wiki":   ("wiki.query",       "low",    False),
    "map":    ("repo.scan",        "low",    False),
    "generate-doc-nodes": ("docs.generate_and_verify_program_nodes", "low", False),
    "verify-doc-nodes": ("docs.verify_documentation_nodes", "low", False),
    "list-doc-targets": ("docs.list_program_targets", "low", False),
    "refresh-nodes": ("docs.refresh_node_grid", "low", False),
    "self-improve-dry-run": ("self_improvement.dry_run_goal", "low", False),
    "self-improve": ("self_improvement.execute_goal", "medium", False),
    "self-test": ("system.self_test", "medium", False),
}

#: Known supported commands for error messages
SUPPORTED_COMMANDS = sorted(_COMMAND_ROUTES.keys())


# ---------------------------------------------------------------------------
# CommandParser
# ---------------------------------------------------------------------------


class CommandParser:
    """
    Converts a slash command string into a GovernedAction.

    Parsing rules:
      • Input must start with '/'.
      • The first token after '/' is the command name.
      • Everything after the command name is the argument string.
      • For /goal and /queue, the full argument becomes goal_text.
      • For /halt and /revert, no arguments are expected.
      • Unknown commands produce ParseResult(success=False).

    Risk override:
      The RiskClassifier may upgrade the base risk level based on
      argument content (e.g., a /shell command with 'rm -rf' is critical).
    """

    def parse(self, raw_input: str) -> ParseResult:
        """
        Parse *raw_input* (a slash command string) into a ParseResult.

        Parameters
        ----------
        raw_input:
            The full message text, e.g. '/goal "scan the codebase"'.

        Returns
        -------
        ParseResult
            success=True with a GovernedAction, or success=False with
            an error message and suggestion.
        """
        stripped = raw_input.strip()

        if not stripped.startswith("/"):
            return ParseResult(
                success=False,
                error="Not a slash command — input must start with '/'.",
                suggestion=f"Did you mean one of: {', '.join('/' + c for c in SUPPORTED_COMMANDS)}?",
            )

        # Split into command + argument
        parts = stripped[1:].split(None, 1)
        command = parts[0].lower() if parts else ""
        arg_string = parts[1].strip() if len(parts) > 1 else ""

        if not command:
            return ParseResult(
                success=False,
                error="Empty command after '/'.",
                suggestion=f"Supported: {', '.join('/' + c for c in SUPPORTED_COMMANDS)}",
            )

        if command not in _COMMAND_ROUTES:
            return ParseResult(
                success=False,
                error=f"Unknown command: /{command}",
                suggestion=f"Supported commands: {', '.join('/' + c for c in SUPPORTED_COMMANDS)}",
            )

        rpc_method, base_risk, base_confirm = _COMMAND_ROUTES[command]

        # Parse command-specific arguments
        args = self._parse_args(command, arg_string)

        # Risk classification override — check arg content
        risk_level = self._classify_risk(command, rpc_method, arg_string, base_risk)
        requires_confirmation = base_confirm or (risk_level in ("high", "critical"))

        # Deterministic action ID
        action_id = generate_deterministic_id(f"cmd:{command}:{arg_string}")

        action = GovernedAction(
            action_id=action_id,
            command=command,
            raw_input=raw_input,
            args=args,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            routed_to=rpc_method,
            is_batch=(command == "queue"),
        )

        log.info(
            "CommandParser: /%s -> %s [risk=%s, confirm=%s]",
            command, rpc_method, risk_level, requires_confirmation,
        )
        return ParseResult(success=True, action=action)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_args(self, command: str, arg_string: str) -> dict[str, Any]:
        """Extract structured arguments based on command type."""
        args: dict[str, Any] = {}

        if command in ("goal", "phase", "queue"):
            # Strip surrounding quotes if present
            goal_text = re.sub(r'^["\']|["\']$', "", arg_string).strip()
            args["goal_text"] = goal_text
            args["is_overnight"] = "--overnight" in arg_string or "--batch" in arg_string

        elif command == "halt":
            # Optional: /halt <task_id>
            if arg_string:
                args["task_id"] = arg_string.strip()

        elif command == "undo":
            args["checkpoint_name"] = arg_string.strip() or "undo-last-command"

        elif command == "revert":
            args["checkpoint_name"] = arg_string.strip() or "checkpoint"

        elif command == "shell":
            args["command"] = arg_string.strip()

        elif command == "wiki":
            # /wiki query <search terms>  or  /wiki <search terms>
            parts = arg_string.split(None, 1)
            if parts and parts[0] in ("query", "search", "find"):
                args["query"] = parts[1].strip() if len(parts) > 1 else ""
            else:
                args["query"] = arg_string.strip()

        elif command in ("scan", "lint", "status", "report", "map", "build"):
            # No arguments needed for these
            args["raw"] = arg_string

        elif command in ("generate-doc-nodes", "verify-doc-nodes"):
            targets = [part.strip() for part in arg_string.split() if part.strip()]
            args["targets"] = targets

        elif command == "list-doc-targets":
            args["raw"] = arg_string

        elif command == "refresh-nodes":
            args["raw"] = arg_string

        elif command == "self-improve-dry-run":
            args["goal"] = arg_string.strip()
            args["target_files"] = ["README.md"]

        elif command == "self-improve":
            args["goal"] = arg_string.strip()

        elif command == "self-test":
            args["include_audit"] = "--no-audit" not in arg_string

        elif command in ("fix", "test"):
            args["target"] = arg_string.strip()

        return args

    def _classify_risk(
        self,
        command: str,
        rpc_method: str,
        arg_string: str,
        base_risk: str,
    ) -> str:
        """Override base risk if argument content is dangerous."""
        # Shell commands always go through RiskClassifier
        if command == "shell" and arg_string:
            classified = RiskClassifier.classify_action("execute_command", arg_string)
            # Take the higher of base and classified
            risk_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
            if risk_order.get(classified, 0) > risk_order.get(base_risk, 0):
                return classified

        # Revert/halt on a production path are critical
        if command in ("revert", "undo") and ("main" in arg_string or "prod" in arg_string):
            return "critical"

        return base_risk

```

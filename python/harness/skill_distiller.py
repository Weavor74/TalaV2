"""
skill_distiller.py — Tala Cognitive Engine Room: Autonomous Skill Distiller
===========================================================================
Monitors successful agent task completions and distills them into reusable
"Skills" — structured YAML manifests whose steps are derived directly from
the workflow execution log.

Each step in the produced manifest maps to one log entry and carries:
  - action: the normalised action type (infer / shell / mcp_tool / memory /
             doc / workflow / custom)
  - label:  short human-readable description
  - detail: raw log message for traceability

The YAML is immediately marketplace-ready: callers should pass it to
``marketplace.publish`` to anchor it on the Tala PoA blockchain.

Milestone 4 — Task 4.1.1
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Action classifier
# ---------------------------------------------------------------------------

_INFER_PATTERNS = re.compile(
    r"(generat|infer|complet|stream|prompt|llm|model|token)", re.IGNORECASE
)
_SHELL_PATTERNS = re.compile(
    r"(shell|bash|command|exec|run |subprocess|script)", re.IGNORECASE
)
_MCP_PATTERNS = re.compile(
    r"(mcp|tool call|tool result|server)", re.IGNORECASE
)
_MEMORY_PATTERNS = re.compile(
    r"(memory|recall|search|reindex|node|retriev)", re.IGNORECASE
)
_DOC_PATTERNS = re.compile(
    r"(document|html.nn|neural.node|skill.node|learning.node)", re.IGNORECASE
)
_WORKFLOW_PATTERNS = re.compile(
    r"(workflow|step|trigger|gate|phase|distill)", re.IGNORECASE
)


def _classify_action(text: str) -> str:
    """Map a log message to a canonical skill action type."""
    if _INFER_PATTERNS.search(text):
        return "infer"
    if _SHELL_PATTERNS.search(text):
        return "shell"
    if _MCP_PATTERNS.search(text):
        return "mcp_tool"
    if _MEMORY_PATTERNS.search(text):
        return "memory"
    if _DOC_PATTERNS.search(text):
        return "doc"
    if _WORKFLOW_PATTERNS.search(text):
        return "workflow"
    return "custom"


def _normalise_entry(entry: Any) -> str:
    """Return a plain string from a log entry regardless of its type."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        # Accept dicts like {"message": "...", "action": "..."}
        for key in ("message", "msg", "log", "detail", "text", "action"):
            if key in entry and isinstance(entry[key], str):
                return entry[key].strip()
        return str(entry)
    return str(entry).strip()


# ---------------------------------------------------------------------------
# SkillDistiller
# ---------------------------------------------------------------------------


class SkillDistiller:
    """
    Autonomous workflow that monitors successful agent task completions and
    distills them into reusable "Skills" (YAML manifests).

    The produced YAML is marketplace-ready and can be anchored to the Tala
    Proof-of-Authority blockchain via ``marketplace.publish``.
    """

    def __init__(self, skills_dir: str = "skills") -> None:
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def distill_workflow(
        self,
        task_description: str,
        execution_log: list,
        success: bool,
    ) -> str | None:
        """
        Analyse a workflow execution log and save a reusable skill manifest.

        Parameters
        ----------
        task_description:
            Human-readable description of the goal that was completed.
        execution_log:
            Ordered list of log entries.  Each entry may be a plain string or
            a dict with at minimum a ``"message"`` key.
        success:
            If ``False`` the distillation is skipped — only successful runs
            produce transferable skills.

        Returns
        -------
        str | None
            Absolute path to the saved ``.yaml`` file, or ``None`` if skipped
            or an error occurred.
        """
        if not success:
            logger.info("Task was not successful; skipping skill distillation.")
            return None

        skill_id = f"skill_{uuid.uuid4().hex[:8]}"
        skill_path = self.skills_dir / f"{skill_id}.yaml"

        steps = self._build_steps(execution_log)

        skill_def: dict[str, Any] = {
            "name": self._make_name(task_description),
            "description": task_description,
            "version": "1.0.0",
            "type": "workflow",
            "marketplace_ready": True,
            "steps": steps,
            # Embed a truncated snapshot of the raw log so reviewers can
            # verify provenance without re-running the workflow.
            "execution_log_snapshot": self._snapshot(execution_log),
        }

        try:
            with open(skill_path, "w", encoding="utf-8") as f:
                yaml.dump(skill_def, f, allow_unicode=True, sort_keys=False)
            logger.info("Distilled new skill '%s' to %s", skill_id, skill_path)
            # The UI can now parse this YAML and optionally call
            # marketplace.publish to anchor it to the Tala PoA chain.
            return str(skill_path)
        except Exception as exc:
            logger.error("Failed to distill skill: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_name(description: str) -> str:
        words = description.strip().split()
        short = " ".join(words[:8])
        return f"Skill: {short}{'…' if len(words) > 8 else ''}"

    @staticmethod
    def _build_steps(execution_log: list) -> list[dict[str, Any]]:
        """
        Convert each log entry into a typed skill step dict.

        Consecutive entries that resolve to the same action type are kept
        as separate steps to preserve execution fidelity.
        """
        if not execution_log:
            return [{"action": "custom", "label": "Workflow completed", "detail": ""}]

        steps: list[dict[str, Any]] = []
        for entry in execution_log:
            text = _normalise_entry(entry)
            if not text:
                continue
            action = _classify_action(text)
            # Truncate detail to 200 chars to keep the YAML readable.
            steps.append({
                "action": action,
                "label": text[:80] + ("…" if len(text) > 80 else ""),
                "detail": text[:200],
            })

        return steps or [{"action": "custom", "label": "Workflow completed", "detail": ""}]

    @staticmethod
    def _snapshot(execution_log: list, max_entries: int = 50) -> list[str]:
        """Return up to *max_entries* normalised log lines for provenance."""
        result: list[str] = []
        for entry in execution_log[:max_entries]:
            text = _normalise_entry(entry)
            if text:
                result.append(text[:200])
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

skill_distiller = SkillDistiller()

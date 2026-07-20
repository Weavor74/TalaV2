"""
test_self_documenter.py - Auto-generated documentation base.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sdk.agent.self_documenter import SelfDocumenter


def test_document_completed_goal_writes_learning_node(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    dummy_anchor = ("hash", "chain", "anchored", {}, "pubkey")

    with patch("sdk.agent.self_documenter.canonicalize_and_anchor", return_value=dummy_anchor):
        with patch("sdk.agent.documentation_nodes.canonicalize_and_anchor", return_value=dummy_anchor):
            documenter = SelfDocumenter(str(tmp_path))
            result = documenter.document_completed_goal(
                goal_text="Learn provider routing",
                phases_completed=["inspect settings", "confirm fallback"],
                task_id="task-123",
                changed_files=["sdk/agent/documentation_nodes.py"],
                tests_status="passed",
            )

    skill_node_path = Path(result["skill_node_path"])
    learning_node_path = Path(result["learning_node_path"])

    assert skill_node_path.exists()
    assert learning_node_path.exists()
    assert learning_node_path.name.startswith("learning-")

    learning_html = learning_node_path.read_text(encoding="utf-8")
    assert 'data-doc-target="learning.event"' in learning_html
    assert "Canonical Topic Node" in learning_html
    assert "Learn provider routing" in learning_html

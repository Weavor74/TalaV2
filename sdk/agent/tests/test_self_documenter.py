"""
test_self_documenter.py - Auto-generated documentation base.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sdk.agent.self_documenter import SelfDocumenter


def test_document_completed_goal_writes_skill_node_but_not_a_learning_node(
    tmp_path, monkeypatch
):
    """
    Completing a goal is not the same as learning something.

    This path used to mint a learning node for *every* finished goal, titled
    with the raw goal text, and it was the dominant source of junk pages in the
    wiki -- of 17 accumulated learning nodes only three carried a real lesson,
    and most of the rest were task restatements from benchmark runs.

    The skill node still records that the work happened. Only the "lesson" is
    withheld, by assess_learning_quality rejecting the completion boilerplate
    this caller passes as its summary.
    """
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
    assert skill_node_path.exists(), "the skill node is the record that work happened"

    assert not result["learning_node_path"], (
        "a completed goal must not auto-mint a learning node from boilerplate"
    )
    assert not list(tmp_path.rglob("learning-*.html"))


def test_a_substantive_lesson_still_becomes_a_learning_node(tmp_path, monkeypatch):
    """The gate withholds boilerplate, not genuine lessons."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    dummy_anchor = ("hash", "chain", "anchored", {}, "pubkey")

    from sdk.agent.documentation_nodes import create_learning_documentation_node

    with patch("sdk.agent.documentation_nodes.canonicalize_and_anchor", return_value=dummy_anchor):
        result = create_learning_documentation_node(
            topic="Provider fallback ordering",
            summary=(
                "Remote providers are tried before local and embedded ones, so a "
                "reachable-but-wrong remote silently shadows a correct local model; "
                "health checks must assert on which provider actually answered."
            ),
            details=["Order is remote -> local -> embedded", "Tier rank lives in server.py"],
        )

    assert result.get("status") != "skipped", result
    learning_node_path = Path(result["path"])
    assert learning_node_path.exists()
    assert learning_node_path.name.startswith("learning-")

    html = learning_node_path.read_text(encoding="utf-8")
    assert 'data-doc-target="learning.event"' in html
    assert "Provider fallback ordering" in html

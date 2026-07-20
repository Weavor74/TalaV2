"""
test_learning_documentation.py - Auto-generated documentation base.
"""

from __future__ import annotations

from pathlib import Path

from sdk.agent.documentation_nodes import create_learning_documentation_node


def test_create_learning_documentation_node_creates_unique_event_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    first = create_learning_documentation_node(
        topic="Provider selection",
        summary="Tala learned to prefer local inference backends when available.",
        canonical_node_id="system-tala-configurable-provider-surface",
        details=["Prefer local first", "Fall back to remote endpoints"],
        source_files=["sdk/agent/documentation_nodes.py"],
    )
    second = create_learning_documentation_node(
        topic="Provider selection",
        summary="Tala learned to prefer local inference backends when available.",
        canonical_node_id="system-tala-configurable-provider-surface",
        details=["Prefer local first", "Fall back to remote endpoints"],
        source_files=["sdk/agent/documentation_nodes.py"],
    )

    assert first["validation_passed"] is True
    assert second["validation_passed"] is True
    assert first["node_id"] != second["node_id"]

    first_path = Path(first["path"])
    second_path = Path(second["path"])
    assert first_path.exists()
    assert second_path.exists()
    assert first_path != second_path

    first_html = first_path.read_text(encoding="utf-8")
    assert 'data-doc-target="learning.event"' in first_html
    assert "Canonical Topic Node" in first_html
    assert "Durable learning event captured by Tala." in first_html

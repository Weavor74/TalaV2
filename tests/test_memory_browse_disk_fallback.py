"""Highs H8: memory.browse for nodes must fall back to REPO_ROOT/nodes HTML."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from python.harness import memory as mem


@pytest.mark.asyncio
async def test_browse_node_falls_back_to_disk_html(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    html = nodes / "concept-highs-h8.html"
    html.write_text(
        "<!DOCTYPE html><html><head><title>H8</title></head>"
        "<body><article>disk-only context for highs H8</article></body></html>",
        encoding="utf-8",
    )

    class _Settings:
        REPO_ROOT = tmp_path

    monkeypatch.setattr("python.harness.config.settings", _Settings(), raising=False)
    # Also patch where memory imports settings inside the helper
    import python.harness.config as cfg
    monkeypatch.setattr(cfg, "settings", _Settings())

    monkeypatch.setattr(mem.db, "fetchrow", AsyncMock(return_value=None))

    result = await mem.handle_memory_browse(
        {"source_type": "node", "ref_id": "concept-highs-h8"}
    )
    assert "context" in result
    ctx = result["context"]
    assert "disk-only context for highs H8" in ctx or "concept-highs-h8" in ctx
    assert "No detailed context available" not in ctx


@pytest.mark.asyncio
async def test_browse_node_honest_error_when_missing(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes"
    nodes.mkdir()

    class _Settings:
        REPO_ROOT = tmp_path

    import python.harness.config as cfg
    monkeypatch.setattr(cfg, "settings", _Settings())
    monkeypatch.setattr(mem.db, "fetchrow", AsyncMock(return_value=None))

    result = await mem.handle_memory_browse(
        {"source_type": "neural-node", "ref_id": "does-not-exist"}
    )
    assert "not in the nodes table" in result["context"]
    assert "REPO_ROOT/nodes" in result["context"]


@pytest.mark.asyncio
async def test_browse_node_prefers_db_content(monkeypatch):
    monkeypatch.setattr(
        mem.db,
        "fetchrow",
        AsyncMock(return_value={"content_json": {"id": "n1", "body": "from-db"}}),
    )
    result = await mem.handle_memory_browse({"source_type": "node", "ref_id": "n1"})
    payload = json.loads(result["context"])
    assert payload["body"] == "from-db"

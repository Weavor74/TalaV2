"""
tests/test_research_crawl_persists_node.py
============================================
Regression coverage for the research.crawl_and_summarize gap: the handler
prompted the LLM to generate a proper HTML-NN concept node but never wrote
it to nodes/ itself. Unlike the manual "Save Selected as Neural Node" UI
button (research.distill_to_node), the autonomous crawl-and-summarize path
the model calls mid-chat has no separate client-side file:write step -- if
the harness doesn't persist the node here, the well-formed HTML-NN the LLM
just produced evaporates the moment the tool result is consumed as chat
context. See html-nn-universal-data-mandate in project memory.
"""
from unittest.mock import AsyncMock, patch

import pytest

from python.harness.server import (
    extract_html_nn_document,
    handle_research_crawl_and_summarize,
    resolve_nodes_dir,
)

_VALID_NODE_HTML = """```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Neural Node: Widgets</title></head>
<body>
<article data-node-id="concept-widgets" data-node-type="concept" data-execution-authority="0.00">
  <header id="node-identity"><h1 id="node-title">Widgets</h1></header>
  <section id="semantic-memory"><h2>Semantic Memory</h2><p>Widgets are useful.</p></section>
  <nav id="relationship-anchors"><h2>Relationships</h2><ul></ul></nav>
</article>
</body>
</html>
```"""


def test_extract_html_nn_document_strips_fence_and_validates():
    assert extract_html_nn_document(_VALID_NODE_HTML) is not None
    assert "<!DOCTYPE html>" in extract_html_nn_document(_VALID_NODE_HTML)
    assert "```" not in extract_html_nn_document(_VALID_NODE_HTML)


def test_extract_html_nn_document_rejects_prose_response():
    # The model ignored the formatting instructions and just chatted --
    # must not be treated as a real node.
    prose = "Sure! Widgets are small mechanical devices used in many industries."
    assert extract_html_nn_document(prose) is None


def test_extract_html_nn_document_handles_empty_and_none():
    assert extract_html_nn_document("") is None
    assert extract_html_nn_document(None) is None


def test_resolve_nodes_dir_respects_tala_user_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    nodes_dir = resolve_nodes_dir()
    assert nodes_dir == tmp_path / "nodes"
    assert nodes_dir.exists()


@pytest.mark.asyncio
async def test_crawl_and_summarize_persists_generated_node(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    with patch("python.harness.server.fetch_webpage_text", new_callable=AsyncMock) as mock_fetch, \
         patch("python.harness.server.execute_llm_generation", new_callable=AsyncMock) as mock_llm, \
         patch("python.harness.memory.handle_memory_reindex_node", new_callable=AsyncMock) as mock_reindex:
        mock_fetch.return_value = "Widgets are small mechanical devices."
        mock_llm.return_value = _VALID_NODE_HTML

        response = await handle_research_crawl_and_summarize(
            {"urls": ["https://example.test/widgets"], "subject": "Widgets"},
            req_id=1,
        )

    import json
    payload = json.loads(response)
    result = payload["result"]

    assert result["persisted"] is True
    assert result["persist_error"] is None
    assert result["node_id"] == "concept-widgets"
    assert result["node_path"]
    assert result["node_path"].startswith(str(tmp_path)), (
        f"node escaped test isolation: {result['node_path']!r} is not under {tmp_path!r}"
    )

    written = (tmp_path / "nodes")
    written_files = list(written.glob("concept-widgets-*.html"))
    assert len(written_files) == 1
    assert "data-node-id=\"concept-widgets\"" in written_files[0].read_text(encoding="utf-8")
    mock_reindex.assert_awaited_once()


@pytest.mark.asyncio
async def test_crawl_and_summarize_does_not_persist_non_html_response(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    with patch("python.harness.server.fetch_webpage_text", new_callable=AsyncMock) as mock_fetch, \
         patch("python.harness.server.execute_llm_generation", new_callable=AsyncMock) as mock_llm:
        mock_fetch.return_value = "Widgets are small mechanical devices."
        mock_llm.return_value = "Here is a summary of widgets: they are useful."

        response = await handle_research_crawl_and_summarize(
            {"urls": ["https://example.test/widgets"], "subject": "Widgets"},
            req_id=1,
        )

    import json
    payload = json.loads(response)
    result = payload["result"]

    assert result["persisted"] is False
    assert result["node_path"] is None
    assert result["persist_error"]
    assert not list((tmp_path / "nodes").glob("*.html"))

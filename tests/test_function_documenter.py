import json
import pytest
from unittest.mock import patch

from sdk.agent.function_documenter import document_function, document_registered_tools


def sample_tool_function(query: str) -> dict:
    """A trivial function used purely as a documentation target in tests."""
    return {"query": query}


def _mock_inference_response(text: str) -> str:
    return json.dumps({"result": {"text": text}})


@pytest.mark.asyncio
async def test_document_function_creates_node_with_both_explanations(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    mocked_text = (
        "TECHNICAL: Wraps the input query string in a single-key dict and returns it.\n"
        "PLAIN: This function is to help refine searches."
    )
    with patch(
        "python.harness.server.handle_inference_generate",
        return_value=_mock_inference_response(mocked_text),
    ) as mock_llm:
        result = await document_function(func=sample_tool_function, doc_target="tool.registry")

    assert result["success"] is True
    assert result["changed"] is True
    assert mock_llm.await_count == 1
    assert result["technical_explanation"] == "Wraps the input query string in a single-key dict and returns it."
    assert result["plain_explanation"] == "This function is to help refine searches."

    node_path = tmp_path / "nodes" / f"{result['node_id']}.html"
    assert node_path.exists()
    html = node_path.read_text(encoding="utf-8")
    assert "Wraps the input query string in a single-key dict" in html
    assert "This function is to help refine searches." in html
    assert 'data-node-type="function-doc"' in html


@pytest.mark.asyncio
async def test_document_function_skips_unchanged_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    mocked_text = "TECHNICAL: Does a thing.\nPLAIN: This does a thing for you."
    with patch(
        "python.harness.server.handle_inference_generate",
        return_value=_mock_inference_response(mocked_text),
    ) as mock_llm:
        first = await document_function(func=sample_tool_function, doc_target="tool.registry")
        second = await document_function(func=sample_tool_function, doc_target="tool.registry")

    assert first["changed"] is True
    assert second["changed"] is False
    # The LLM should only be called once — the second call short-circuits on
    # an unchanged source fingerprint.
    assert mock_llm.await_count == 1


@pytest.mark.asyncio
async def test_document_function_force_regenerates_even_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    mocked_text = "TECHNICAL: Does a thing.\nPLAIN: This does a thing for you."
    with patch(
        "python.harness.server.handle_inference_generate",
        return_value=_mock_inference_response(mocked_text),
    ) as mock_llm:
        await document_function(func=sample_tool_function, doc_target="tool.registry")
        forced = await document_function(func=sample_tool_function, doc_target="tool.registry", force=True)

    assert forced["changed"] is True
    assert mock_llm.await_count == 2


@pytest.mark.asyncio
async def test_document_function_falls_back_when_model_output_unparseable(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    with patch(
        "python.harness.server.handle_inference_generate",
        return_value=_mock_inference_response("I refuse to follow the format."),
    ):
        result = await document_function(func=sample_tool_function, doc_target="tool.registry")

    assert result["success"] is True
    assert "pending" in result["technical_explanation"].lower()
    assert "pending" in result["plain_explanation"].lower()


@pytest.mark.asyncio
async def test_document_registered_tools_reports_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    from sdk.agent.tools import ToolRegistry

    ToolRegistry._tools["_fd_test_tool"] = {
        "name": "_fd_test_tool",
        "description": "test",
        "schema": {},
        "func": sample_tool_function,
        "plugin_id": None,
    }
    try:
        mocked_text = "TECHNICAL: Does a thing.\nPLAIN: This does a thing for you."
        with patch(
            "python.harness.server.handle_inference_generate",
            return_value=_mock_inference_response(mocked_text),
        ):
            summary = await document_registered_tools(tool_names=["_fd_test_tool"])
    finally:
        ToolRegistry._tools.pop("_fd_test_tool", None)

    assert summary["success"] is True
    assert summary["changed"] == 1
    assert summary["unchanged"] == 0
    assert summary["errors"] == []

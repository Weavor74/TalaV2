"""
tests/test_skill_synthesis.py
==============================
Regression coverage for sdk.agent.skill_synthesis.synthesize_python_skill --
the shared LLM-based code-synthesis step used by both GoalEngine and
WorkflowExecutor when distilling a completed run into a skill HTML Neural
Node.

Traced live: workflow-derived skill nodes (nodes/skill-*.html written by
WorkflowExecutor._distill_workflow_skill) never carried a
<script type="application/x-python" id="node-executable"> block, because
that code path never attempted synthesis at all -- unlike
GoalEngine._auto_document_skill, which already called an (until now)
private, untested equivalent. Without embedded code,
ToolRegistry.auto_discover_skills() (sdk/agent/tools.py) has nothing to
exec() and register as a callable tool, so the "skill" is permanently inert
documentation that can never be chained into a future workflow.

The LLM call is always mocked here -- these tests must stay fast and
deterministic regardless of whether a local model backend is running.
"""
from unittest.mock import AsyncMock, patch

import pytest

from sdk.agent.skill_synthesis import synthesize_python_skill

_VALID_CODE_RESPONSE = '''```python
from typing import Any
from sdk.agent.tools import register_tool

@register_tool(name="do_the_thing", description="Does the thing")
async def do_the_thing() -> dict[str, Any]:
    """Does the thing."""
    return {"status": "success"}
```'''


@pytest.mark.asyncio
async def test_successful_synthesis_returns_cleaned_code():
    with patch("sdk.agent.llm_coder.LLMCodeGenerator._call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _VALID_CODE_RESPONSE
        code = await synthesize_python_skill("Do the thing", ["Step one", "Step two"])

    assert code is not None
    assert "@register_tool(name=\"do_the_thing\"" in code
    assert "```" not in code  # markdown fence must be stripped


@pytest.mark.asyncio
async def test_empty_llm_response_returns_none():
    with patch("sdk.agent.llm_coder.LLMCodeGenerator._call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = None
        code = await synthesize_python_skill("Do the thing", ["Step one"])

    assert code is None


@pytest.mark.asyncio
async def test_code_without_extractable_tool_name_returns_none():
    with patch("sdk.agent.llm_coder.LLMCodeGenerator._call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "```python\nprint('no register_tool call here')\n```"
        code = await synthesize_python_skill("Do the thing", ["Step one"])

    assert code is None


@pytest.mark.asyncio
async def test_llm_exception_is_caught_and_returns_none():
    with patch("sdk.agent.llm_coder.LLMCodeGenerator._call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = RuntimeError("model backend unreachable")
        code = await synthesize_python_skill("Do the thing", ["Step one"])

    assert code is None

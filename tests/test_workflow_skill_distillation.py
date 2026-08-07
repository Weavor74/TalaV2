"""
test_workflow_skill_distillation.py - Auto-generated documentation base.
"""

from unittest.mock import AsyncMock, patch

import pytest

from python.harness.workflow_executor import WorkflowExecutor
from sdk.agent.parser import parse_file

_WORKFLOW_SKILL_SMOKE = {
    "flow_id": "skill_smoke",
    "name": "Skill Smoke",
    "nodes": [
        {
            "id": "trigger",
            "type": "trigger",
            "label": "Start",
            "connections": {"onSuccess": "output"},
        },
        {
            "id": "output",
            "type": "output",
            "label": "Finish",
        },
    ],
}


@pytest.mark.asyncio
async def test_successful_workflow_distills_to_html_nn_skill(tmp_path, monkeypatch):
    # Belt-and-suspenders isolation alongside root_dir=tmp_path below:
    # confirmed live that a fake "Skill Smoke" skill node from this exact
    # test ended up sitting in the real %APPDATA%\tala-engine\nodes\ (with
    # real execution authority, picked up by unrelated live chat requests
    # via keyword overlap), so root_dir alone isn't being trusted as
    # sufficient here without also pinning TALA_USER_DATA.
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    # Code synthesis now goes through a real (mocked-out-here) LLM call via
    # sdk.agent.skill_synthesis.synthesize_python_skill -- see
    # test_workflow_distillation_embeds_synthesized_code below for the
    # "synthesis succeeds" path. Here we simulate "no code available",
    # matching this test's original behavior, and keeping it fast/
    # deterministic instead of depending on a live model backend.
    with patch(
        "sdk.agent.skill_synthesis.synthesize_python_skill",
        new_callable=AsyncMock,
    ) as mock_synth:
        mock_synth.return_value = None
        result = await WorkflowExecutor(_WORKFLOW_SKILL_SMOKE, root_dir=tmp_path).execute({})

    skill_path = result.get("distilled_skill_node")
    assert result["status"] == "COMPLETED"
    assert skill_path
    assert skill_path.endswith(".html")
    assert skill_path.startswith(str(tmp_path)), (
        f"skill node escaped test isolation: {skill_path!r} is not under {tmp_path!r}"
    )
    assert not list((tmp_path / "skills").glob("*.yaml"))

    parsed = parse_file(skill_path)
    assert parsed.node_type == "skill"
    assert parsed.node_id.startswith("skill-workflow_skill_smoke")
    assert "Workflow: Skill Smoke" in parsed.semantic_memory
    assert "Finish (output): Workflow outputs compiled." in parsed.semantic_memory
    assert parsed.executable_code is None


@pytest.mark.asyncio
async def test_workflow_distillation_embeds_synthesized_code(tmp_path, monkeypatch):
    # Closes the gap traced from the live calculator/HTML-NN incident:
    # WorkflowExecutor._distill_workflow_skill previously never attempted
    # code synthesis at all (unlike GoalEngine._auto_document_skill), so
    # workflow-derived skill nodes could never carry a node-executable
    # script and could never be auto-discovered/registered as a real,
    # chainable tool by ToolRegistry.auto_discover_skills(). This asserts
    # the wiring: when synthesis succeeds, the resulting skill node's
    # <script type="application/x-python" id="node-executable"> block
    # actually contains the synthesized code.
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    synthesized_code = (
        'from typing import Any\n'
        'from sdk.agent.tools import register_tool\n\n'
        '@register_tool(name="skill_smoke_tool", description="Auto-synthesized from Skill Smoke")\n'
        'async def skill_smoke_tool() -> dict[str, Any]:\n'
        '    """Auto-synthesized skill."""\n'
        '    return {"status": "success"}\n'
    )

    with patch(
        "sdk.agent.skill_synthesis.synthesize_python_skill",
        new_callable=AsyncMock,
    ) as mock_synth:
        mock_synth.return_value = synthesized_code
        result = await WorkflowExecutor(_WORKFLOW_SKILL_SMOKE, root_dir=tmp_path).execute({})

    skill_path = result.get("distilled_skill_node")
    assert result["status"] == "COMPLETED"
    assert skill_path

    parsed = parse_file(skill_path)
    assert parsed.executable_code is not None
    assert "@register_tool(name=\"skill_smoke_tool\"" in parsed.executable_code

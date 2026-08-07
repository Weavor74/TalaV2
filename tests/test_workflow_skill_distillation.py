"""
test_workflow_skill_distillation.py - Auto-generated documentation base.
"""

import pytest

from python.harness.workflow_executor import WorkflowExecutor
from sdk.agent.parser import parse_file


@pytest.mark.asyncio
async def test_successful_workflow_distills_to_html_nn_skill(tmp_path, monkeypatch):
    # Belt-and-suspenders isolation alongside root_dir=tmp_path below:
    # confirmed live that a fake "Skill Smoke" skill node from this exact
    # test ended up sitting in the real %APPDATA%\tala-engine\nodes\ (with
    # real execution authority, picked up by unrelated live chat requests
    # via keyword overlap), so root_dir alone isn't being trusted as
    # sufficient here without also pinning TALA_USER_DATA.
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    workflow = {
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

    result = await WorkflowExecutor(workflow, root_dir=tmp_path).execute({})

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

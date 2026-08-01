# File: `test_workflow_skill_distillation.py`

## Functions

### `async def test_successful_workflow_distills_to_html_nn_skill(tmp_path)`
*No docstring provided.*

## Source Code

```python
import pytest

from python.harness.workflow_executor import WorkflowExecutor
from sdk.agent.parser import parse_file


@pytest.mark.asyncio
async def test_successful_workflow_distills_to_html_nn_skill(tmp_path):
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
    assert not list((tmp_path / "skills").glob("*.yaml"))

    parsed = parse_file(skill_path)
    assert parsed.node_type == "skill"
    assert parsed.node_id.startswith("skill-workflow_skill_smoke")
    assert "Workflow: Skill Smoke" in parsed.semantic_memory
    assert "Finish (output): Workflow outputs compiled." in parsed.semantic_memory

```

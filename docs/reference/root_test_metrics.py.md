# File: `test_metrics.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
import os
import asyncio
from pathlib import Path
import json
from sdk.agent.tools import ToolRegistry
from sdk.agent.self_documenter import SelfDocumenter

nodes_dir = Path(os.path.join(os.environ.get("TALA_WORKSPACE_ROOT", "."), "nodes"))
nodes_dir.mkdir(exist_ok=True)

fake_skill_html = """<!DOCTYPE html>
<html>
<head><meta name="tala-schema" content="neural-node/v1"></head>
<body>
<article data-node-id="skill-test_mock_metric" data-node-type="skill" data-execution-authority="0.20">
<script type="application/json" id="node-state">
{
  "metrics": {
    "version": 1,
    "success_count": 0,
    "failure_count": 0,
    "success_rate": 0.0,
    "confidence": 0.8
  }
}
</script>
<section id="history"></section>
<script type="application/x-python" id="node-executable">
from sdk.agent.tools import register_tool
@register_tool(name="test_mock_metric", description="A mock skill for testing metrics")
async def test_mock_metric(succeed: bool):
    if not succeed:
        raise ValueError("Intentional failure")
    return "Success"
</script>
</article>
</body>
</html>"""

node_path = nodes_dir / 'skill-test_mock_metric.html'
node_path.write_text(fake_skill_html, encoding='utf-8')

async def main():
    ToolRegistry.auto_discover_skills()
    print('Running success case...')
    await ToolRegistry.run_tool('test_mock_metric', {'succeed': True})
    print('Running failure case...')
    await ToolRegistry.run_tool('test_mock_metric', {'succeed': False})
    print('Running success case 2...')
    await ToolRegistry.run_tool('test_mock_metric', {'succeed': True})
    
    final_html = node_path.read_text(encoding='utf-8')
    state_str = final_html.split('<script type="application/json" id="node-state">')[1].split('</script>')[0].strip()
    print('Final Node State:')
    print(state_str)
    
asyncio.run(main())

```

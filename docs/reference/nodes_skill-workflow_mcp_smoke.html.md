# File: `skill-workflow_mcp_smoke.html`

## Overview

A neural node HTML template used as a system prompt to test basic MCP functionality and tool calling.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Skill Node: Workflow: MCP Smoke</title>
</head>
<body>
<article
  data-node-id="skill-workflow_mcp_smoke"
  data-node-type="skill"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.20"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.80"
  data-chaincode-id="unanchored"
  data-content-hash="809266fe95b880943a4ec6664845e835b272a31144109e0403a19aa457dd6d56"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">

  <header id="node-identity">
    <h1 id="node-title">Skill: Workflow: MCP Smoke</h1>
  </header>

  <section id="semantic-memory">

    <p>Goal: Workflow: MCP Smoke</p>
    <p>Phases: trigger (trigger): Trigger captured input payload. | tool (mcp_tool): MCP tool 'echo' executed.</p>
    <p>Keywords: captured, executed, input, payload, smoke, trigger, workflow</p>
    <p>Tests: Workflow completed successfully</p>
    
    <h2>Procedure (Latest Recipe)</h2>
    <ol>
      <li>trigger (trigger): Trigger captured input payload.</li><li>tool (mcp_tool): MCP tool 'echo' executed.</li>
    </ol>
    
    <h2>Run History</h2>
    
  <article class="skill-run" data-task-id="workflow-mcp-smoke" data-recorded="2026-06-26T20:13:51.869961+00:00">
    <h3>Run: <time datetime="2026-06-26T20:13:51.869961+00:00">2026-06-26 20:13:51 UTC</time></h3>
    <p><strong>Task ID:</strong> <code>workflow-mcp-smoke</code></p>
    <p><strong>Phases (2):</strong> trigger (trigger): Trigger captured input payload. | tool (mcp_tool): MCP tool 'echo' executed.</p>
    <p><strong>Tests:</strong> Workflow completed successfully</p>
    <ol>
      <li>trigger (trigger): Trigger captured input payload.</li><li>tool (mcp_tool): MCP tool 'echo' executed.</li>
    </ol>
    <p><strong>Artifacts:</strong></p>
    <ul>
      <li><code>workflows/mcp-smoke.json</code></li>
    </ul>
  </article>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul></ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "goal": "Workflow: MCP Smoke",
  "phases": [
    "trigger (trigger): Trigger captured input payload.",
    "tool (mcp_tool): MCP tool 'echo' executed."
  ],
  "task_id": "workflow-mcp-smoke",
  "phase_count": 2,
  "changed_files": [
    "workflows/mcp-smoke.json"
  ],
  "tests_status": "Workflow completed successfully",
  "recorded_at": "2026-06-26T20:13:51.869961+00:00",
  "metrics": {
    "version": 1,
    "success_count": 0,
    "failure_count": 0,
    "success_rate": 0.0,
    "confidence": 0.8
  }
}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{
  "activation_keywords": [],
  "tool_affordances": [],
  "forbidden_actions": [],
  "repair_policies": {},
  "validators": {},
  "confidence": 1.0
}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
{
  "core_hash": "sha256:809266fe95b880943a4ec6664845e835b272a31144109e0403a19aa457dd6d56",
  "chaincode_id": "unanchored",
  "anchoring_status": "failed",
  "raw_response": "<urlopen error timed out>",
  "key_fingerprint": "unanchored",
  "parent_chaincode_ids": [],
  "created_by": "tala-goal-engine",
  "created_at": "2026-06-26T20:13:51.869961+00:00",
  "wrapper_hash": "sha256:17a7f24fda921878813405d6c00efb81de6c281a1b2ed1ca8a544755199a74ec"
}
  </script>


</article>
</body>
</html>

```

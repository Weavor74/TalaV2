"""
workflow_compiler.py — Tala Cognitive Engine Room: Workflow to HTML-NN Compiler
================================================================================
Compiles a `tala-flow/1.0` JSON workflow structure into a valid HTML Neural Node
(HTML-NN) that can be parsed and indexed by the cognitive graph.

Milestone 17 — Task 17.1.1
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

def compile_workflow_to_html(workflow: dict[str, Any]) -> str:
    """
    Compile a workflow JSON configuration into a standard HTML Neural Node.

    Parameters
    ----------
    workflow : dict
        A parsed JSON object representing a `tala-flow/1.0` workflow.

    Returns
    -------
    str
        The compiled HTML-NN document.
    """
    flow_id = workflow.get("flow_id") or str(uuid.uuid4())
    name = workflow.get("name") or "Unnamed Workflow"
    nodes = workflow.get("nodes") or []
    description = workflow.get("description", "")
    tags: list[str] = workflow.get("tags") or []
    trigger_keywords: list[str] = workflow.get("trigger_keywords") or []

    # Generate human-readable semantic summary description of the workflow capabilities
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    node_desc_lines = []
    for node in nodes:
        node_type = node.get("type", "unknown")
        node_label = node.get("label", f"Node ({node_type})")
        node_id = node.get("id", "unknown")
        node_cfg_desc = node.get("config", {}).get("description", "")

        line = (
            f"* **{node_label}** (Type: `{node_type}`, ID: `{node_id}`): "
            f"Connects to onSuccess -> `{node.get('connections', {}).get('onSuccess')}`."
        )
        if node_cfg_desc:
            line += f" {node_cfg_desc}"
        node_desc_lines.append(line)

    nodes_summary = "\n      ".join(node_desc_lines)

    # Build the semantic memory — include tags and trigger keywords for TF-IDF indexing
    tags_line = ", ".join(tags) if tags else ""
    keywords_line = ", ".join(trigger_keywords) if trigger_keywords else ""

    semantic_memory = f"""This is a compiled workflow node named '{name}'.
      {description}
      When activated in the cognitive net, it allows the agent to execute a sequential pipeline of actions.

      **Workflow Nodes Checklist:**
      {nodes_summary}

      This workflow can be executed by invoking the RPC method `workflow.execute` with the Flow ID `flow-{flow_id}`.
      It carries operational authority and runs tasks in the defined sequence."""

    if tags_line:
        semantic_memory += f"\n\n      **Tags:** {tags_line}"
    if keywords_line:
        semantic_memory += f"\n\n      **Trigger Keywords:** {keywords_line}"

    # Build relationship anchors (e.g. linking back to whoami or default agents)
    relationships_html = ""
    # Link to default whoami
    relationships_html += """
      <li>
        <a href="../../sdk/identity/whoami.html" data-relationship-type="implements" data-relationship-weight="1.00">
          Whoami Master Identity Node
        </a>
      </li>"""

    # Escape JSON contents for safe HTML script block rendering
    workflow_escaped = json.dumps(workflow, indent=2, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v1">
  <meta name="tala-node-id" content="flow-{flow_id}">
  <meta name="tala-node-type" content="workflow">
  <meta name="tala-authority" content="1.00">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="{now_iso}">
  <title>Workflow: {name}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; color: #e2e8f0; background-color: #04060b; }}
    article {{ border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 6px; padding: 1.5rem; background-color: #0b0f19; }}
    section {{ margin-top: 1.5rem; }}
    code {{ background: rgba(255, 255, 255, 0.1); padding: 0.1rem 0.3rem; border-radius: 3px; font-family: monospace; }}
  </style>
</head>
<body>

<article
  data-node-id="flow-{flow_id}"
  data-node-type="workflow"
  data-execution-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00">

  <header id="node-identity">
    <h1 id="node-title">Workflow: {name}</h1>
    <dl>
      <dt>Flow ID</dt>
      <dd><code>flow-{flow_id}</code></dd>
      <dt>Type</dt>
      <dd><code>workflow</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>1.00</code></dd>
      <dt>Created</dt>
      <dd><time datetime="{now_iso}">{now_iso}</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>
      {semantic_memory}
    </p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>{relationships_html}
    </ul>
  </nav>

  <script type="application/json" id="node-state">
  {workflow_escaped}
  </script>

</article>

</body>
</html>
"""
    return html_content

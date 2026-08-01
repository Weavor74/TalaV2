# File: `system-tala-self-updating-documentation-contract.html`

## Overview

Defines the agent's contract for keeping its own documentation up-to-date as it modifies code.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-self-updating-documentation-contract">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Self-Updating Documentation Contract</title>
</head>
<body>
<article
  data-node-id="system-tala-self-updating-documentation-contract"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.96"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="1.00"
  data-governance-status="context-only"
  data-doc-target="tala.self-updating-documentation"
  data-source-files="README.md;nodes/system-self-improvement-loop.html;nodes/system-neural-node-v2-schema.html;nodes/system-html-nn-authoring-guide.html;nodes/system-program-documentation-generator.html;nodes/system-tala-html-nn-memory-runtime.html;sdk/agent/documentation_nodes.py;sdk/agent/tools.py;python/harness/server.py;python/harness/memory.py"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-self-updating-documentation-contract"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Self-Updating Documentation Contract</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-self-updating-documentation-contract</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Defines the mandatory rule that every Tala code, schema, provider, workflow, guardrail, UI, packaging, or runtime change must update the matching HTML-NN reference node in the same change.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala uses HTML Neural Nodes as her primary repair reference. Documentation is not an afterthought: it is part of the operating memory that tells Tala how to understand, test, and fix herself. A code change that changes behavior without updating the relevant HTML-NN node leaves Tala with stale self-knowledge.</p>
    <p>The contract is simple: when Tala changes herself, she must update the closest matching documentation node, add a new node when no correct home exists, validate the resulting HTML-NN structure, and update <code>README.md</code> whenever a new top-level architecture reference is created. When Tala learns durable new information, she must also create a fresh learning-event node and link it to the canonical topic node so the graph records the new memory instead of overwriting history. Completed turns that express a durable lesson should be promoted into that same learning-node flow automatically.</p>
  </section>

  <section id="mandatory-update-rule">
    <h2>Mandatory Update Rule</h2>
    <ol>
      <li>Identify every changed domain: UI surface, JSON-RPC handler, database table, provider contract, MCP server, OAuth/auth path, workflow, guardrail, agent builder, astro engine, packaging, self-test, memory, or gateway behavior.</li>
      <li>Open the existing HTML-NN node for each domain before editing, or create a new documentation node under <code>nodes/</code> if no accurate node exists.</li>
      <li>Update semantic memory, operational contracts, repair checklists, source-file metadata, and machine-readable <code>node-state</code> or <code>director</code> blocks.</li>
      <li>Keep documentation truthful: do not claim a feature, route, installer, model, or self-test exists unless it exists in code or is explicitly marked planned.</li>
      <li>Run node validation and, when behavior changed, run the smallest relevant self-test or command test.</li>
      <li>Record validation status in the final handoff and never report completion until files are written and parsed.</li>
    </ol>
  </section>

  <section id="approved-update-paths">
    <h2>Approved Update Paths</h2>
    <table>
      <thead>
        <tr>
          <th>Need</th>
          <th>Preferred Update Path</th>
          <th>Validation</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Create one feature reference</td>
          <td><code>docs.create_documentation_node</code> or direct repository edit</td>
          <td>Parse HTML, verify metadata, verify JSON blocks</td>
        </tr>
        <tr>
          <td>Regenerate program docs</td>
          <td><code>docs.generate_program_nodes</code></td>
          <td><code>docs.verify_documentation_nodes</code></td>
        </tr>
        <tr>
          <td>Repair stale wiki/memory text</td>
          <td><code>wiki.update</code> or <code>memory.save_node</code></td>
          <td>Reload memory index and retrieve the new node</td>
        </tr>
        <tr>
          <td>Add a top-level reference</td>
          <td>New <code>nodes/system-*.html</code> plus <code>README.md</code> link</td>
          <td>README link opens and node parses</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section id="source-metadata-contract">
    <h2>Source Metadata Contract</h2>
    <p>Every repair reference must carry enough metadata for Tala to decide whether the node is stale. The <code>article</code> element should include <code>data-source-files</code>, <code>data-source-fingerprint</code>, <code>data-generated-at</code>, <code>data-doc-target</code>, <code>data-governance-status</code>, authority values, trust tier, and privacy classification.</p>
    <ul>
      <li><code>data-source-files</code> lists the files used to author the node.</li>
      <li><code>data-source-fingerprint</code> changes when the documented source materially changes.</li>
      <li><code>data-generated-at</code> records when the node was last updated.</li>
      <li><code>data-governance-status="context-only"</code> means the node can guide reasoning but cannot execute actions by itself.</li>
    </ul>
  </section>

  <section id="repair-checklist">
    <h2>Repair Checklist</h2>
    <ul>
      <li>If a new JSON-RPC method is added, update <code>system-tala-rpc-surface-reference</code>.</li>
      <li>If a table or persisted field changes, update <code>system-tala-database-schema-reference</code>.</li>
      <li>If provider, model, OAuth, MCP, gateway, or admin behavior changes, update <code>system-tala-configurable-provider-surface</code> and <code>system-tala-operator-control-plane</code>.</li>
      <li>If embedded inference, dependencies, or first-run setup changes, update <code>system-tala-embedded-inference-runtime</code> and <code>system-tala-install-packaging-runtime</code>.</li>
      <li>If self-testing changes, update <code>system-self-improvement-loop</code> and install/runtime docs.</li>
      <li>If the change creates a new top-level architecture domain, add a README architecture link.</li>
    </ul>
  </section>

  <section id="anti-patterns">
    <h2>Anti-Patterns</h2>
    <ul>
      <li>Do not bury feature truth only in chat history; Tala retrieves HTML-NN files, not promises.</li>
      <li>Do not document automatic provider selection when the product contract is user-configured provider/model selection.</li>
      <li>Do not claim installers download model weights unless first-run or startup code actually forces that behavior.</li>
      <li>Do not leave docs describing an old endpoint name after renaming the JSON-RPC handler.</li>
      <li>Do not use a documentation node as permission to bypass guardrails, credentials, or operator approval.</li>
    </ul>
  </section>

  <section id="relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="system-self-improvement-loop.html" data-relationship="governs">Self-Improvement Loop</a></li>
      <li><a href="system-tala-rpc-surface-reference.html" data-relationship="requires-updates">RPC Surface Reference</a></li>
      <li><a href="system-tala-database-schema-reference.html" data-relationship="requires-updates">Database Schema Reference</a></li>
      <li><a href="system-tala-embedded-inference-runtime.html" data-relationship="requires-updates">Embedded Inference Runtime</a></li>
      <li><a href="system-tala-operator-control-plane.html" data-relationship="requires-updates">Operator Control Plane</a></li>
    </ul>
  </section>

  <script type="application/json" id="node-state">
  {
    "schema": "tala-node-state/v1",
    "node_id": "system-tala-self-updating-documentation-contract",
    "status": "active",
    "contract": {
      "documentation_required_for_behavior_changes": true,
      "new_top_level_nodes_require_readme_link": true,
      "validation_required_before_completion_claim": true,
      "authority": "context-only"
    },
    "update_domains": [
      "ui",
      "rpc",
      "database",
      "provider",
      "mcp",
      "oauth",
      "workflow",
      "guardrail",
      "agent_builder",
      "astro",
      "packaging",
      "self_test",
      "memory",
      "gateway"
    ]
  }
  </script>

  <script type="application/json" id="director">
  {
    "schema": "tala-director/v1",
    "intent": "self_repair_documentation_governance",
    "on_change": [
      "load_relevant_documentation_node",
      "update_or_create_html_nn_reference",
      "validate_html_nn_metadata_and_json",
      "update_readme_if_new_top_level_node",
      "run_relevant_self_test"
    ],
    "forbidden_claims": [
      "documentation updated without file mutation",
      "feature exists without source evidence",
      "node valid without parser verification"
    ]
  }
  </script>
</article>
</body>
</html>

```

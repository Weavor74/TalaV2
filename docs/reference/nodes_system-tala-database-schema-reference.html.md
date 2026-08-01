# File: `system-tala-database-schema-reference.html`

## Overview

A persistent reference node that injects the PostgreSQL schema into the agent's context.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-database-schema-reference">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Database Schema Reference</title>
</head>
<body>
<article
  data-node-id="system-tala-database-schema-reference"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.95"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.94"
  data-governance-status="context-only"
  data-doc-target="tala.database.schema"
  data-source-files="python/harness/db.py;python/harness/server.py;scripts/first_run.py"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-database-schema-reference"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Database Schema Reference</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-database-schema-reference</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Documents the SQLite persistence backbone used by Tala for nodes, providers, agents, guardrails, profiles, contacts, tasks, memory, research, and runtime configuration.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala persists operator configuration and local memory in the harness database initialized by <code>python/harness/db.py</code>. The schema is part of the self-repair surface: a UI panel, RPC handler, or command that saves state must have a corresponding table, row contract, migration behavior, and documentation entry.</p>
    <p>The embedded inference provider is seeded by default as a locked local provider record. This does not make Tala an automatic model chooser; it gives the operator a last-resort built-in provider that becomes usable when model weights are cached.</p>
  </section>

  <section id="core-memory-tables">
    <h2>Core Memory Tables</h2>
    <ul>
      <li><code>nodes</code> stores HTML-NN node metadata and content references.</li>
      <li><code>token_ledger</code> and <code>wallet</code> track token accounting and local wallet state.</li>
      <li><code>agent_log</code> records agent events and operational notes.</li>
      <li><code>conversation_turns</code>, <code>memory_index</code>, <code>research_results</code>, and <code>memory_recall_events</code> support conversation memory, retrieval, research, and recall telemetry.</li>
      <li><code>heartbeats</code> records runtime heartbeat state for local/remote liveness.</li>
    </ul>
  </section>

  <section id="operator-configuration-tables">
    <h2>Operator Configuration Tables</h2>
    <ul>
      <li><code>inference_providers</code> stores operator-defined provider records plus the locked <code>embedded-builtin</code> provider.</li>
      <li><code>agents</code> stores agent/persona records, configuration, and metadata used by Agent Astro.</li>
      <li><code>guardrails_config</code> stores guardrail policy and builder output.</li>
      <li><code>client_settings</code> stores desktop client preferences and runtime options.</li>
      <li><code>search_providers</code>, <code>git_providers</code>, <code>storage_providers</code>, <code>database_providers</code>, and <code>runtime_instances</code> store configurable external service endpoints.</li>
      <li><code>auth_credentials</code> stores credential references for API keys, OAuth, SSH/RDP, gateway, and provider authentication.</li>
    </ul>
  </section>

  <section id="identity-trust-tables">
    <h2>Identity and Trust Tables</h2>
    <ul>
      <li><code>profiles</code> stores user profile records, including birth metadata for User Profile Astro.</li>
      <li><code>contacts</code> stores the contacts directory used by social, peer, and blockchain trust flows.</li>
      <li><code>firewall_contacts</code> stores blockchain firewall and trust matrix contact policy.</li>
      <li><code>p2p_sessions</code> and <code>p2p_messages</code> store peer connection and message history.</li>
    </ul>
  </section>

  <section id="task-workflow-tables">
    <h2>Task and Workflow Tables</h2>
    <ul>
      <li><code>tasks</code> stores mission control tasks, state, owner, priority, and structured metadata.</li>
      <li><code>task_event_store</code> stores task lifecycle events for replay and debugging.</li>
      <li><code>goal_decompositions</code> stores decomposed plans and dependencies for multi-step agent work.</li>
    </ul>
  </section>

  <section id="repair-rules">
    <h2>Repair Rules</h2>
    <ol>
      <li>When adding a persistent setting, prefer a focused table or JSON metadata field consistent with adjacent tables.</li>
      <li>When changing a table shape, update initialization and any read/write handler in the same change.</li>
      <li>When adding provider-like configuration, keep enabled state, priority, endpoint/model/config metadata, and auth binding explicit.</li>
      <li>When adding user/identity/contact fields, preserve local-first privacy and avoid leaking secrets into plain documentation.</li>
      <li>When changing database behavior, update this node and the RPC surface reference before claiming self-repair is complete.</li>
    </ol>
  </section>

  <section id="relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="system-tala-rpc-surface-reference.html" data-relationship="serves">RPC Surface Reference</a></li>
      <li><a href="system-tala-operator-control-plane.html" data-relationship="persists">Operator Control Plane</a></li>
      <li><a href="system-tala-html-nn-memory-runtime.html" data-relationship="indexes">HTML-NN Memory Runtime</a></li>
      <li><a href="system-tala-self-updating-documentation-contract.html" data-relationship="updated-by">Self-Updating Documentation Contract</a></li>
    </ul>
  </section>

  <script type="application/json" id="node-state">
  {
    "schema": "tala-node-state/v1",
    "node_id": "system-tala-database-schema-reference",
    "status": "active",
    "schema_source": "python/harness/db.py",
    "table_groups": {
      "core_memory": ["nodes", "token_ledger", "wallet", "agent_log", "heartbeats", "conversation_turns", "memory_index", "research_results", "memory_recall_events"],
      "operator_configuration": ["inference_providers", "agents", "guardrails_config", "client_settings", "search_providers", "git_providers", "storage_providers", "database_providers", "runtime_instances", "auth_credentials"],
      "identity_trust": ["profiles", "contacts", "firewall_contacts", "p2p_sessions", "p2p_messages"],
      "tasks": ["tasks", "task_event_store", "goal_decompositions"]
    },
    "default_records": {
      "embedded_provider": "embedded-builtin"
    }
  }
  </script>
</article>
</body>
</html>

```

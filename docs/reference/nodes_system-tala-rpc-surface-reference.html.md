# File: `system-tala-rpc-surface-reference.html`

## Overview

Injects documentation regarding the JSON-RPC interface and WebSocket API structure.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-rpc-surface-reference">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala RPC Surface Reference</title>
</head>
<body>
<article
  data-node-id="system-tala-rpc-surface-reference"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.94"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.95"
  data-governance-status="context-only"
  data-doc-target="tala.rpc.surface"
  data-source-files="python/harness/server.py;renderer/renderer.js;sdk/agent/command_parser.py;python/harness/db.py"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-rpc-surface-reference"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala RPC Surface Reference</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-rpc-surface-reference</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Catalogs Tala's JSON-RPC control surface so self-repair work can find the correct handler family before changing UI, backend, providers, memory, workflows, or gateway behavior.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala's renderer communicates with the Python harness through named JSON-RPC methods registered in <code>python/harness/server.py</code>. The RPC table is the backend contract for provider configuration, chat generation, memory, workflows, guardrails, agents, MCP, OAuth/auth, user profiles, contacts, blockchain setup, gateway health, command execution, self-testing, and repository recovery.</p>
    <p>This node is a repair map, not an execution capability. If a UI surface breaks, Tala should first identify the relevant method family, inspect the corresponding handler, verify database persistence, then update the matching documentation node when behavior changes.</p>
  </section>

  <section id="core-runtime-rpcs">
    <h2>Core Runtime RPCs</h2>
    <ul>
      <li><code>harness.health</code>, <code>system.info</code>, and <code>system.self_test</code> expose runtime health, environment facts, and Tala's self-test lane.</li>
      <li><code>command.parse</code> routes slash commands through the SDK command parser before invoking backend behavior.</li>
      <li><code>db.query</code> and <code>db.insert</code> support controlled database reads and writes used by the local harness.</li>
      <li><code>token.count</code>, <code>graph.activate</code>, <code>vault.status</code>, and <code>agent.log</code> provide token, memory graph, vault, and agent log primitives.</li>
    </ul>
  </section>

  <section id="inference-provider-rpcs">
    <h2>Inference Provider RPCs</h2>
    <ul>
      <li><code>inference.generate</code> streams model output and honors explicit <code>provider_id</code> when supplied by the session.</li>
      <li><code>inference.get_providers</code> and <code>inference.save_providers</code> load and persist operator-defined provider records.</li>
      <li><code>inference.get_embedded_status</code> and <code>inference.trigger_download</code> expose embedded model cache/download state.</li>
      <li><code>inference.detect_local_providers</code> probes locally available runtimes without changing the user's configured provider choices.</li>
    </ul>
  </section>

  <section id="configuration-rpcs">
    <h2>Configuration RPCs</h2>
    <ul>
      <li><code>providers.*</code> covers generalized provider groups such as search, Git, storage, database, runtime, and auth backends.</li>
      <li><code>mcp.*</code> covers MCP server registry, status, startup, shutdown, discovery, and tool invocation surfaces.</li>
      <li><code>auth.*</code> stores and validates configurable credential references, including API keys, SSH/RDP credentials, and OAuth-style records.</li>
      <li><code>gateway.status</code> reports remote gateway/auth posture for the desktop UI.</li>
    </ul>
  </section>

  <section id="builder-rpcs">
    <h2>Builder RPCs</h2>
    <ul>
      <li><code>guardrails.*</code> backs the guardrail configuration and builder surface.</li>
      <li><code>workflows.*</code> backs workflow creation, validation, execution, and compilation into agent-runnable plans.</li>
      <li><code>agents.*</code> backs agent/persona creation, activation, configuration, and birthdate metadata used by Agent Astro.</li>
      <li><code>tasks.*</code> and <code>swarm.*</code> support mission control, task state, event stores, decomposition, and multi-agent coordination.</li>
    </ul>
  </section>

  <section id="identity-trust-rpcs">
    <h2>Identity and Trust RPCs</h2>
    <ul>
      <li><code>identity.*</code> manages local identity material used for peer trust and local ownership.</li>
      <li><code>profiles.*</code> manages user profile records, including profile birthdate used by User Profile Astro.</li>
      <li><code>contacts.*</code> manages the contacts directory, including blockchain contact metadata and trust information.</li>
      <li><code>p2p.*</code> manages peer sessions and peer messaging.</li>
      <li><code>chain.*</code> manages blockchain ledger, trust, contact, and setup surfaces used by the blockchain firewall.</li>
      <li><code>firewall.*</code> manages trust matrix and contact firewall policy.</li>
    </ul>
  </section>

  <section id="astro-rpcs">
    <h2>Astro RPCs</h2>
    <ul>
      <li><code>astro.transits</code> computes current or predictive transits for <code>agent</code>, <code>user</code>, <code>tala</code>, or <code>custom</code> subjects.</li>
      <li><code>astro.synastry</code> compares User Profile Astro and active Agent Astro, returning whether the relationship effect enhances, conflicts, or mixes.</li>
      <li>Astro output is bounded by style caps and remains advisory; it must not override guardrails, user consent, or provider routing.</li>
    </ul>
  </section>

  <section id="memory-docs-rpcs">
    <h2>Memory and Documentation RPCs</h2>
    <ul>
      <li><code>nodes.*</code>, <code>docs.*</code>, <code>wiki.*</code>, and <code>memory.*</code> manage HTML-NN creation, verification, activation, retrieval, indexing, and wiki updates.</li>
      <li><code>research.*</code>, <code>research_memory.*</code>, and <code>telemetry.*</code> support research capture, distillation, recall events, and observability.</li>
      <li>When changing any method in these families, update this reference and the HTML-NN memory runtime documentation.</li>
    </ul>
  </section>

  <section id="execution-recovery-rpcs">
    <h2>Execution and Recovery RPCs</h2>
    <ul>
      <li><code>shell.*</code>, <code>code.*</code>, <code>test.*</code>, and <code>build.*</code> cover controlled execution, code operations, testing, and build orchestration.</li>
      <li><code>repo.*</code> covers repository checkpoints, restore, diff, and recovery behaviors used by command recovery such as <code>/undo</code>.</li>
      <li><code>plugins.*</code> covers plugin discovery, install/update state, and plugin health.</li>
      <li><code>self_improvement.*</code> covers self-repair workflow state and must stay aligned with documentation update rules.</li>
    </ul>
  </section>

  <section id="repair-rules">
    <h2>Repair Rules</h2>
    <ol>
      <li>Do not add a renderer call until the backend method exists or a stub is deliberately documented as planned.</li>
      <li>Do not add a backend handler without registering it in the handler table.</li>
      <li>Do not add a persisted RPC without checking the matching database table and migrations.</li>
      <li>Do not change a provider/model/auth/MCP contract without updating operator control plane docs.</li>
      <li>Do not claim command recovery works unless checkpoint creation and restore paths are both tested.</li>
    </ol>
  </section>

  <section id="relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="system-tala-agent-architecture.html" data-relationship="implements">Architecture Overview</a></li>
      <li><a href="system-tala-configurable-provider-surface.html" data-relationship="details">Configurable Provider Surface</a></li>
      <li><a href="system-tala-database-schema-reference.html" data-relationship="persists-through">Database Schema Reference</a></li>
      <li><a href="system-tala-self-updating-documentation-contract.html" data-relationship="updated-by">Self-Updating Documentation Contract</a></li>
    </ul>
  </section>

  <script type="application/json" id="node-state">
  {
    "schema": "tala-node-state/v1",
    "node_id": "system-tala-rpc-surface-reference",
    "status": "active",
    "handler_table_source": "python/harness/server.py",
    "categories": {
      "core_runtime": ["harness.health", "system.info", "system.self_test", "command.parse", "db.query", "db.insert"],
      "inference": ["inference.generate", "inference.get_providers", "inference.save_providers", "inference.get_embedded_status", "inference.trigger_download", "inference.detect_local_providers"],
      "configuration": ["providers.*", "mcp.*", "auth.*", "gateway.status"],
      "builders": ["guardrails.*", "workflows.*", "agents.*", "tasks.*", "swarm.*"],
      "identity_trust": ["identity.*", "profiles.*", "contacts.*", "p2p.*", "chain.*", "firewall.*"],
      "astro": ["astro.transits", "astro.synastry"],
      "memory_docs": ["nodes.*", "docs.*", "wiki.*", "memory.*", "research.*", "research_memory.*", "telemetry.*"],
      "execution_recovery": ["shell.*", "code.*", "test.*", "build.*", "repo.*", "plugins.*", "self_improvement.*"]
    }
  }
  </script>
</article>
</body>
</html>

```

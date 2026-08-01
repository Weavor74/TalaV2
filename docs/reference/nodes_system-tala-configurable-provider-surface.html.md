# File: `system-tala-configurable-provider-surface.html`

## Overview

Guides the agent on parsing and modifying inference provider configurations.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-configurable-provider-surface">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Configurable Provider Surface</title>
</head>
<body>
<article
  data-node-id="system-tala-configurable-provider-surface"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.93"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.90"
  data-governance-status="context-only"
  data-doc-target="tala.configurable.providers"
  data-source-files="python/harness/server.py;python/harness/db.py;python/harness/mcp_manager.py;python/harness/search_manager.py;python/harness/settings_nn.py;renderer/renderer.js;sdk/gateway/plugins.py;sdk/gateway/permissions.py"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-configurable-provider-surface"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Configurable Provider Surface</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-configurable-provider-surface</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Map of Tala's runtime-customizable endpoints, providers, MCP servers, auth credentials, firewall contacts, and plugin gateway surfaces.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala's providers and endpoints are customizable runtime data owned by the operator. Tala does not assume it knows which inference provider or model should be used. The user configures provider endpoints, model names, authentication, priority, and fallback behavior. The application stores inference providers, search providers, git providers, storage providers, database providers, runtime instances, authentication credentials, and firewall contacts in PostgreSQL. Renderer settings panels call JSON-RPC methods to load, reorder, edit, and save these collections.</p>
    <p>Inference providers include embedded, local, and remote model backends. Local endpoints can be auto-detected across common OpenAI-compatible local servers, including Ollama, LM Studio, vLLM, and generic local model servers. Endpoint normalization appends a versioned OpenAI-compatible path for local engines when users provide a bare host.</p>
    <p>MCP is configurable through a persisted registry. Tala can register stdio MCP servers with command, args, cwd, and env config, or SSE MCP servers with URL, headers, and timeout config. The MCP manager redacts tokens, secrets, passwords, API keys, and authorization headers when listing configs. Tools are listed and called through the real MCP Python SDK when installed.</p>
    <p>Auth credentials are a first-class provider surface. Tala stores prioritized SSH, RDP, API key, and OAuth-style credential records. OAuth configuration is a manual operator-owned control surface for issuer, endpoints, clients, scopes, secrets, and gateway/provider bindings; it is not a hidden hosted onboarding shortcut. Firewall contacts are also database-backed and describe trusted peer addresses and access rights. User profile, Contacts Directory records, P2P handshakes, and blockchain chain/VPS connection state are part of the same operator-owned trust fabric. These records support local-first operation while allowing explicit remote, peer, and gateway integrations.</p>
    <p>Settings saves update their corresponding private HTML-NN settings nodes. This means provider configuration is not only stored in database tables; it is mirrored into parseable private node memory so Tala can summarize and reason about its current configuration without relying on stale hardcoded assumptions.</p>
  </section>

  <section id="provider-tables">
    <h2>Provider Tables</h2>
    <ul>
      <li><code>inference_providers</code>: model backend type, name, priority, status, locked flag, and JSON config.</li>
      <li><code>search_providers</code>: search endpoint, API key, and priority, with fallback to direct DuckDuckGo HTML crawling.</li>
      <li><code>git_providers</code>: git host endpoint, username, API key, and priority.</li>
      <li><code>storage_providers</code>: heterogeneous storage credentials keyed by provider type.</li>
      <li><code>database_providers</code>: external or replica database endpoint, username, password, database name, and priority.</li>
      <li><code>runtime_instances</code>: remote HTML-NN runtime URLs, API keys, and priority.</li>
      <li><code>auth_credentials</code>: prioritized SSH, RDP, API key, and OAuth credential config.</li>
      <li><code>firewall_contacts</code>: cryptographic peer/contact identifiers and access-rights JSON.</li>
      <li><code>contacts</code>: blockchain wallet contacts, endpoints, usernames, and peer permission metadata.</li>
      <li><code>profiles</code>: human, agent, project, organization, asset, workflow, and memory profile records.</li>
    </ul>
  </section>

  <section id="rpc-surface">
    <h2>RPC Surface</h2>
    <ul>
      <li><code>inference.get_providers</code>, <code>inference.save_providers</code>, <code>inference.detect_local_providers</code>.</li>
      <li><code>search.get_providers</code>, <code>search.save_providers</code>, <code>search.query</code>.</li>
      <li><code>git.get_providers</code>, <code>git.save_providers</code>.</li>
      <li><code>storage.get_providers</code>, <code>storage.save_providers</code>.</li>
      <li><code>database.get_providers</code>, <code>database.save_providers</code>.</li>
      <li><code>runtime.get_instances</code>, <code>runtime.save_instances</code>.</li>
      <li><code>auth.get_credentials</code>, <code>auth.save_credentials</code>.</li>
      <li><code>firewall.get_contacts</code>, <code>firewall.save_contacts</code>.</li>
      <li><code>identity.get_user_profile</code>, <code>identity.save_user_profile</code>.</li>
      <li><code>contacts.list</code>, <code>contacts.save</code>, <code>contacts.delete</code>.</li>
      <li><code>p2p.handshake</code>, <code>p2p.receive_handshake</code>, <code>p2p.send_message</code>, <code>p2p.receive_message</code>.</li>
      <li><code>chain.connection_get</code>, <code>chain.stay_local</code>, <code>chain.disconnect</code>, <code>chain.connect_vps</code>, <code>chain.auto_connect</code>.</li>
      <li><code>profiles.list</code>, <code>profiles.get</code>, <code>profiles.save</code>, <code>profiles.delete</code>.</li>
      <li><code>mcp.connect_server</code>, <code>mcp.list_servers</code>, <code>mcp.remove_server</code>, <code>mcp.list_tools</code>, <code>mcp.call_tool</code>.</li>
      <li><code>plugins.list</code>, <code>plugins.validate</code>, <code>plugins.reload</code>, and <code>gateway.status</code>.</li>
    </ul>
  </section>

  <section id="mcp-contract">
    <h2>MCP Contract</h2>
    <p>Registered MCP servers are opened on demand for each operation. A stdio server requires a command and can include args, cwd, and env. An SSE server requires a URL and can include headers and timeout settings. Tala stores server definitions in a user-data JSON registry and redacts sensitive fields when listing them.</p>
    <pre><code>{
  "name": "filesystem",
  "config": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    "cwd": "D:/src/TalaV2"
  },
  "persist": true
}</code></pre>
  </section>

  <section id="gateway-boundary">
    <h2>Gateway Boundary</h2>
    <p>Plugins and MCP servers are untrusted surfaces. Plugin manifests declare identity, entrypoint, permissions, tools, MCP servers, and trust level. Gateway permission checks enforce declared access before plugin-registered tools run. Promptware defense scans and redacts tool output before it reaches the agent context.</p>
    <p>This node is documentation only. It does not authorize connecting a provider, storing credentials, calling MCP tools, invoking shell commands, or changing gateway policy.</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
      <li>
        <a href="./system-tala-agent-architecture.html"
           data-target-node-id="system-tala-agent-architecture"
           data-relationship-type="supports"
           data-relationship-weight="0.92"
           data-navigation-role="architecture-overview"
           data-authority-required="none">Tala Agent Architecture</a>
      </li>
      <li>
        <a href="./system-tala-html-nn-memory-runtime.html"
           data-target-node-id="system-tala-html-nn-memory-runtime"
           data-relationship-type="supports"
           data-relationship-weight="0.85"
           data-navigation-role="settings-as-memory"
           data-authority-required="none">Tala HTML-NN Memory Runtime</a>
      </li>
      <li>
        <a href="./system-neural-node-v2-schema.html"
           data-target-node-id="system-neural-node-v2-schema"
           data-relationship-type="bounded-by"
           data-relationship-weight="0.90"
           data-navigation-role="schema-contract"
           data-authority-required="none">HTML-NN v2 Schema</a>
      </li>
      <li>
        <a href="./system-program-documentation-generator.html"
           data-target-node-id="system-program-documentation-generator"
           data-relationship-type="references"
           data-relationship-weight="0.70"
           data-navigation-role="documentation-generation"
           data-authority-required="none">Program Documentation Generator Contract</a>
      </li>
    </ul>
  </nav>

  <script type="application/tala-director+json"
          id="director-reference-provider-surface"
          data-script-kind="reference"
          data-side-effects="none">
{
  "when": {
    "intent": "explain_provider_or_mcp_configuration"
  },
  "reference": [
    { "type": "html-nn", "node_id": "system-tala-configurable-provider-surface", "required": true },
    { "type": "html-nn", "node_id": "system-tala-agent-architecture", "required": false }
  ],
  "then": {
    "instruction": "Explain configurable Tala endpoints as runtime provider records and MCP server configs, not as hardcoded-only settings."
  }
}
  </script>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "title": "Tala Configurable Provider Surface",
  "description": "Documentation for Tala's database-backed provider, endpoint, MCP, auth, firewall, plugin, and gateway configuration surfaces.",
  "tags": [
    "documentation",
    "providers",
    "mcp",
    "auth",
    "endpoints",
    "settings",
    "gateway",
    "plugins"
  ],
  "metadata": {
    "author": "codex-review",
    "created": "2026-06-08T00:00:00Z",
    "updated": "2026-06-08T00:00:00Z",
    "version": "1.0.0",
    "schema": "neural-node/v2",
    "source_fingerprint": "sha256:manual-review-2026-06-08-tala-configurable-provider-surface"
  },
  "provider_tables": [
    "inference_providers",
    "search_providers",
    "git_providers",
    "storage_providers",
    "database_providers",
    "runtime_instances",
    "auth_credentials",
    "firewall_contacts"
  ],
  "mcp_transports": [
    "stdio",
    "sse"
  ],
  "verification": {
    "parser_required": true,
    "execution_authority": "0.00",
    "side_effects": "none"
  }
}
  </script>
</article>
</body>
</html>

```

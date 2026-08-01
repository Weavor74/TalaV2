# File: `system-tala-operator-control-plane.html`

## Overview

The primary identity and capability prompt for the top-level operator agent.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-operator-control-plane">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Operator Control Plane</title>
</head>
<body>
<article
  data-node-id="system-tala-operator-control-plane"
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
  data-weight-governance-importance="0.92"
  data-governance-status="context-only"
  data-doc-target="tala.operator.control-plane"
  data-source-files="renderer/index.html;renderer/renderer.js;python/harness/server.py;python/harness/guardrails_manager.py;python/harness/workflow_executor.py;python/harness/settings_nn.py;sdk/agent/workflow_compiler.py;python/harness/db.py;python/astrology/ephemeris.py;python/astrology/style_cap.py;python/astrology/synastry.py;docs/vps-peer.md;scripts/add-vps-contact.py"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-operator-control-plane"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Operator Control Plane</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-operator-control-plane</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Defines Tala as an explicit operator-configured agent workbench with provider, OAuth, MCP, guardrail, workflow, agent, astro-emotional, user profile, contacts, blockchain trust, and HTML-NN control-plane surfaces.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala is an operator-controlled agent workbench. It is not designed to hide infrastructure decisions behind an automatic portal, automatic model catalog, or opaque hosted setup. The user chooses providers, endpoints, models, credentials, OAuth configuration, MCP servers, memory locations, guardrails, workflows, and agents. Tala's job is to preserve those choices, validate them, expose their health, route through them consistently, and document them as governed memory.</p>
    <p>The central product shape is a desktop control plane. Settings panels and builder surfaces are first-class parts of the application: User Profile, Agent, Inference Providers, Workflows, Guardrails, Search Providers, MCP, Git Providers, Storage Providers, Database Providers, Remote HTML-NN Instances, Authentication and Keys, Blockchain Firewall, Contacts Directory, Profiles, Logs, Mission Control, Blockchain Ledger, Agent Builder, and Astro-Emotional Engine. These surfaces are configuration and governance tools, not onboarding decorations.</p>
    <p>When comparing Tala to Hermes Agent, the correct distinction is not that Tala lacks a setup philosophy. Tala has a different philosophy. Hermes emphasizes polished guided entry and broad product lifecycle. Tala emphasizes explicit configuration, local-first authority, HTML-NN memory, and operator-owned routing of agent infrastructure.</p>
  </section>

  <section id="manual-provider-contract">
    <h2>Manual Provider Contract</h2>
    <p>Tala's inference provider surface expects the operator to know what provider and model they want to use. The model picker and session routing controls are configuration conveniences; they must not imply that Tala owns model selection. A configured provider record should preserve the user's explicit endpoint, model name, API shape, token budget, priority, enabled state, and any authentication reference.</p>
    <ul>
      <li><code>renderer/index.html</code> presents Inference Providers and a session-level provider/model selector.</li>
      <li><code>renderer/renderer.js</code> persists the selected session provider and sends <code>provider_id</code> with chat requests.</li>
      <li><code>python/harness/server.py</code> honors per-request <code>provider_id</code> and otherwise follows the user-defined priority chain.</li>
      <li>Fallback behavior should be transparent and inspectable. It may route after failure or token exhaustion, but it should not silently replace the user's chosen model with a different automatic recommendation.</li>
    </ul>
  </section>

  <section id="oauth-auth-control-plane">
    <h2>OAuth and Auth Control Plane</h2>
    <p>Tala's OAuth section exists because authentication is configurable infrastructure. OAuth is not a hidden hosted sign-in shortcut. The operator should be able to define OAuth/OIDC or API credentials for specific providers, gateways, MCP servers, remotes, shell targets, or external services.</p>
    <p>The Authentication and Keys panel covers SSH private keys, RDP credentials, API keys, and OAuth 2.0 access setups. Correct maturity means better manual credential configuration, validation, binding, status telemetry, and redaction.</p>
    <ul>
      <li>OAuth config should include issuer, authorization endpoint, token endpoint, client ID, client secret, scopes, redirect URI, audience/resource, grant type, and refresh policy where applicable.</li>
      <li>Credentials should bind explicitly to a provider, gateway, MCP server, runtime instance, or task class rather than being globally magical.</li>
      <li>Secrets should remain masked on read and preserved when the UI sends masked placeholders back to the backend.</li>
      <li>Status surfaces should report configured, validated, expired, missing-scope, unreachable, and redacted states without exposing secrets.</li>
    </ul>
  </section>

  <section id="guardrail-builder">
    <h2>Guardrail Builder</h2>
    <p>Tala has a guardrail configuration and builder surface under Settings. It exposes preset categories and custom validation rules. Guardrails are stored in durable state, mirrored into settings HTML-NN memory, and applied during inference.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Guardrails panel with category cards for none, lite, moderate, heavy, and custom configurations.</li>
      <li><code>renderer/renderer.js</code> implements <code>GuardrailsManager</code>, loads <code>guardrails.get</code>, and saves <code>guardrails.save</code>.</li>
      <li><code>python/harness/server.py</code> exposes <code>guardrails.get</code> and <code>guardrails.save</code>.</li>
      <li><code>python/harness/guardrails_manager.py</code> loads active guardrails and validates prompts and outputs.</li>
      <li>Pre-inference prompt validation and post-inference output validation run through the active guardrail manager.</li>
    </ul>
    <p>The next maturity step is a preview/test mode: the operator enters sample input/output and sees which rule would block, rewrite, warn, or pass before saving.</p>
  </section>

  <section id="workflow-builder">
    <h2>Workflow Builder</h2>
    <p>Tala has a workflow builder that combines a visual canvas, JSON editor, import/export, run controls, execution logs, and compiled HTML-NN workflow memory. Workflows are not merely settings; saved workflows become graph-activatable cognitive artifacts.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Workflow Automation dashboard, visual builder, JSON editor, palette, node properties, and execution console.</li>
      <li><code>renderer/renderer.js</code> implements <code>WorkflowManager</code>, including list, create, edit, import, export, save, delete, run, canvas sync, JSON sync, and execution-step logs.</li>
      <li><code>python/harness/server.py</code> exposes <code>workflow.list</code>, <code>workflow.save</code>, <code>workflow.delete</code>, and <code>workflow.execute</code>.</li>
      <li><code>sdk/agent/workflow_compiler.py</code> compiles saved workflow JSON into HTML Neural Nodes.</li>
      <li><code>python/harness/workflow_executor.py</code> executes workflows node by node and can emit step telemetry back to the renderer.</li>
      <li>Activated workflow nodes can auto-evaluate during graph/inference context assembly when they pass the activation threshold.</li>
    </ul>
    <p>The next maturity step is executable readiness per node: missing credential, missing provider, invalid endpoint, invalid JSON, disconnected edge, unsafe tool, and unbound auth should show before runtime.</p>
  </section>

  <section id="agent-builder">
    <h2>Agent and Persona Builder</h2>
    <p>Tala has both an Agent settings panel and an Agent Builder workspace. These surfaces configure persona identity, role, crew type, swarm size, system directive, backstory/history HTML-NN, knowledge association tag, and active agent selection. This makes agents user-defined workforce units rather than hidden internal prompts.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Agent settings panel with persona fields and the Agent Builder workspace with Workers, Topologies, Task Board, and Runs tabs.</li>
      <li><code>renderer/renderer.js</code> implements <code>AgentManager</code> and <code>AgentBuilder</code> initialization, active agent selection, persona CRUD, and chat mode state.</li>
      <li><code>python/harness/server.py</code> exposes <code>agent.get_agents</code>, <code>agent.save_agent</code>, <code>agent.delete_agent</code>, <code>agent.set_active</code>, <code>agent.get_chat_mode</code>, and <code>agent.set_chat_mode</code>.</li>
      <li><code>python/harness/settings_nn.py</code> can save agent persona nodes and update settings memory nodes.</li>
      <li>Agent memory can be constrained through background/history node references and HTML-NN tag associations.</li>
    </ul>
    <p>The next maturity step is explicit binding: each agent should clearly show its chosen provider/model, credential set, OAuth/auth binding, guardrail category, allowed workflows, MCP permissions, memory scope, and runtime status.</p>
  </section>

  <section id="astro-emotional-engine">
    <h2>Astro-Emotional / Chrono-Astrology Engine</h2>
    <p>Tala includes an Astro-Emotional Engine that can compute planetary transits, compare user-agent chart resonance, and apply bounded response-style modulation. The engine is not a source of execution authority, provider choice, database permission, wallet authority, or policy override. It is a capped tone/persona modulation system tied to explicit birthdate sources and explicit astrology commands.</p>
    <ul>
      <li><code>renderer/index.html</code> exposes an Agent birthdate field labeled Birthdate (Astrology), an Astrology log category, and an About capability named Astro-Emotional Engine.</li>
      <li><code>renderer/renderer.js</code> saves agent birthdate into persona config, exposes <code>/astrology</code>, calls <code>astro.transits</code>, and renders active transit summaries as System Chrono-Astrology messages.</li>
      <li><code>python/harness/server.py</code> exposes <code>astro.transits</code>, loads user, agent, Tala-origin, or custom birthdate sources, computes current or predictive transits, and applies chrono-astrology style modulation during inference context assembly.</li>
      <li><code>python/harness/server.py</code> exposes <code>astro.synastry</code>, comparing the master user profile chart against the active agent chart and returning whether the relationship tends to enhance, conflict, or mix the two astro patterns.</li>
      <li><code>python/astrology/ephemeris.py</code> uses Swiss Ephemeris when available and falls back to a deterministic approximation when the native library is unavailable.</li>
      <li><code>python/astrology/style_cap.py</code> enforces <code>MAX_ASTRO_INFLUENCE = 0.10</code> and rejects style deltas that attempt to touch sensitive concepts such as code, wallet, database, keys, permissions, execution, or tokens.</li>
      <li><code>python/astrology/synastry.py</code> is part of the astrology package and supports relationship-style astrological comparison logic.</li>
    </ul>
    <p>The astro model has three primary lanes plus one bridge: User Profile Astro describes the human operator's birthdate profile; Agent Astro describes the active agent/persona birthdate profile; Predictive Astro computes current or future transit conditions for a selected subject; User-Agent Synastry compares user and agent charts to identify enhancement, conflict, or mixed resonance. The next maturity step is an Astro Engine panel that shows the selected lane, active chart source, current or future target date, transits, synastry effect, influence cap, fallback mode, and exact style deltas before they are inserted into the system context.</p>
  </section>

  <section id="astro-lanes">
    <h2>Astro Lanes</h2>
    <ul>
      <li>User Profile Astro: reads the master user's profile birthdate or PII DOB and treats it as the operator's chart source. It should influence user-facing interpretation, compatibility, timing, and memory context only when explicitly requested or when a configured policy allows it.</li>
      <li>Agent Astro: reads the active agent/persona birthdate and may modulate agent tone within the hard 10% cap. This is the current inference-time modulation lane.</li>
      <li>Predictive Astro: computes transits for a future date or timestamp. It should answer timing, forecast, and planning questions without becoming deterministic authority over tasks or decisions.</li>
      <li>User-Agent Synastry: compares User Profile Astro and Agent Astro. A high harmonious compatibility score can enhance rapport, alignment, and stylistic fit; a challenging score can signal conflict, friction, or productive tension between user and agent styles.</li>
    </ul>
  </section>

  <section id="user-profile">
    <h2>User Profile and Identity</h2>
    <p>Tala has a User Profile surface that defines the local user's identity and personal operating context. The profile is not only cosmetic. It can include core identity, about data, roleplay persona, PII and vault mappings, roles and work data, skills and capabilities, projects and portfolio, connections, privacy preferences, profile completeness, and chain connection status.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the User Profile panel with profile header, avatar, handle, type badge, edit/save controls, chain update and disconnect controls, profile tabs, completeness score, and chain status card.</li>
      <li><code>renderer/renderer.js</code> initializes <code>ProfileManager</code>, loads profile data, and coordinates chain connection updates.</li>
      <li><code>python/harness/server.py</code> exposes <code>identity.get_user_profile</code> and <code>identity.save_user_profile</code>.</li>
      <li>Saving identity compiles user details into profile memory and synchronizes durable profile state.</li>
      <li>The profile can remain local-only or reconnect to a Tala VPS chain depending on explicit operator choice.</li>
    </ul>
    <p>The next maturity step is profile export/import and clearer separation of public identity, private PII, vault paths, AI-readable profile facts, and chain-published identity metadata.</p>
  </section>

  <section id="contacts-directory">
    <h2>Contacts Directory</h2>
    <p>Tala has a Contacts Directory for blockchain wallet-based contacts and peer endpoints. Contacts are not just address-book entries; they are part of the trust and P2P layer used for handshakes, endpoint routing, permissions, and peer communication.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Contacts Directory view with search, add contact, selected-contact workspace, wallet address, endpoint URL, permission controls, connect, save, and delete actions.</li>
      <li><code>renderer/renderer.js</code> implements <code>ContactsManager</code>, loading <code>contacts.list</code>, saving <code>contacts.save</code>, deleting contacts, and triggering <code>p2p.handshake</code>.</li>
      <li><code>python/harness/server.py</code> exposes <code>contacts.list</code>, <code>contacts.save</code>, and <code>contacts.delete</code>.</li>
      <li><code>/peer @username message</code> sends live P2P messages through <code>p2p.send_message</code>.</li>
      <li>Contact endpoints can point to local peers, LAN peers, VPS peers, or other explicitly configured Tala nodes.</li>
    </ul>
    <p>The next maturity step is connection validation per contact: endpoint reachable, wallet matches, handshake succeeds, peer permissions are current, and P2P send path is healthy.</p>
  </section>

  <section id="blockchain-firewall">
    <h2>Blockchain Firewall and Trust Matrix</h2>
    <p>Tala has a Blockchain Firewall and Trust Matrix for trusted peer authorization. This surface controls who can access which categories of HTML-NN memory and what clearance level applies. It is separate from the ordinary contacts directory: contacts are people or peers; firewall contacts are prioritized trust-policy entries.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Blockchain Firewall panel and Add Trusted Contact modal.</li>
      <li><code>renderer/renderer.js</code> implements <code>FirewallManager</code>, using <code>firewall.get_contacts</code> and <code>firewall.save_contacts</code>.</li>
      <li><code>python/harness/server.py</code> exposes <code>firewall.get_contacts</code> and <code>firewall.save_contacts</code>.</li>
      <li>Firewall access rights include clearance levels and allowed node categories such as profile, vault, workflows, blockchain, and other HTML-NN scopes.</li>
      <li>Gateway auth posture reports credential count, authenticated remote count, and trusted contact count.</li>
    </ul>
    <p>The next maturity step is a policy simulator: choose a contact and a node category, then show whether the contact would be allowed, denied, or require higher clearance before any remote memory access is attempted.</p>
  </section>

  <section id="blockchain-ledger-chain-setup">
    <h2>Blockchain Ledger and Chain Setup</h2>
    <p>Tala includes a Proof-of-Authority blockchain ledger view and chain connection controls. The chain layer anchors identity, contacts, local ledger events, profile connection state, P2P handshakes, and optional VPS peer connectivity.</p>
    <ul>
      <li><code>renderer/index.html</code> defines the Blockchain Ledger view and a read-only Blockchain Node terminal tab.</li>
      <li><code>renderer/renderer.js</code> implements <code>BlockchainViewer</code>, displays chain status, handles offline state, and streams blockchain node output into the terminal panel.</li>
      <li><code>python/harness/server.py</code> exposes chain connection methods including <code>chain.connection_get</code>, <code>chain.stay_local</code>, <code>chain.disconnect</code>, <code>chain.connect_vps</code>, and <code>chain.auto_connect</code>.</li>
      <li>The profile chain card lets the operator remain local-only, reconnect automatically, connect to a Tala VPS chain, update chain identity, or disconnect.</li>
      <li><code>docs/vps-peer.md</code>, <code>scripts/start-tala-vps.sh</code>, and <code>deploy/tala-vps.service</code> document the permanent VPS peer path.</li>
    </ul>
    <p>The next maturity step is a unified chain setup wizard that stays operator-owned: local-only, LAN peer, or VPS peer; explicit endpoint; explicit trusted contact import; explicit firewall permissions; explicit connection test.</p>
  </section>

  <section id="admin-surfaces">
    <h2>Admin Surfaces</h2>
    <p>Tala has a broader configurable surface than a normal agent chat app. The Settings view is a control plane for runtime infrastructure, not a preferences page. Current configurable surfaces include:</p>
    <ul>
      <li>Inference Providers: user-defined model endpoints, model names, priorities, token budgets, and session routing.</li>
      <li>Search Providers: configured web search APIs with fallback behavior.</li>
      <li>MCP Servers: stdio and SSE server registry, tool listing, and tool calls.</li>
      <li>Git Providers: GitHub, GitLab, Gitea, Bitbucket, or self-hosted Git endpoints.</li>
      <li>Storage Providers: cloud buckets, local shares, and sync targets.</li>
      <li>Database Providers: local or remote database endpoints and replica choices.</li>
      <li>Runtime Instances: remote HTML-NN instances prioritized for retrieval and graph propagation.</li>
      <li>Authentication and Keys: SSH, RDP, API, OAuth, and gateway credential records.</li>
      <li>Firewall Contacts: trusted peer/contact permissions and HTML-NN access control categories.</li>
      <li>User Profile: local identity, PII/vault mapping, roles, skills, projects, preferences, and chain connection status.</li>
      <li>Contacts Directory: blockchain wallet contacts, endpoints, permissions, P2P handshake, and peer messaging.</li>
      <li>Blockchain Ledger: Proof-of-Authority chain viewer, chain terminal output, local-only mode, VPS connection, and auto-connect state.</li>
      <li>Astro-Emotional Engine: user-profile astro, active-agent astro, predictive transits, user-agent synastry, bounded style modulation, and explicit <code>/astrology</code> inspection.</li>
    </ul>
  </section>

  <section id="operator-polish-targets">
    <h2>Operator Polish Targets</h2>
    <p>The highest-value polish is not automatic provider selection. It is making each manual configuration surface safer, clearer, and easier to verify.</p>
    <ol>
      <li>Add validate/test buttons for provider, OAuth, MCP, runtime, database, storage, and Git records.</li>
      <li>Add redacted import/export bundles for an operator's Tala environment.</li>
      <li>Add status and readiness badges to each builder card and provider row.</li>
      <li>Bind agents to explicit provider/model/auth/guardrail/workflow/MCP scopes.</li>
      <li>Add guardrail preview tests before save.</li>
      <li>Add workflow readiness analysis before execution.</li>
      <li>Add clear error remediation messages that tell the operator what to change.</li>
      <li>Add contact and chain readiness checks for wallet match, endpoint reachability, handshake success, and permission simulation.</li>
      <li>Add an Astro Engine status panel showing Swiss Ephemeris availability, fallback mode, lane selector, user chart, agent chart, predictive date, synastry enhancement/conflict, transit list, and 10% style cap enforcement.</li>
    </ol>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="./system-tala-agent-architecture.html" data-target-node-id="system-tala-agent-architecture" data-relationship-type="extends" data-relationship-weight="0.95" data-navigation-role="architecture-overview" data-authority-required="none">Tala Agent Architecture</a></li>
      <li><a href="./system-tala-configurable-provider-surface.html" data-target-node-id="system-tala-configurable-provider-surface" data-relationship-type="extends" data-relationship-weight="0.95" data-navigation-role="provider-auth-mcp" data-authority-required="none">Tala Configurable Provider Surface</a></li>
      <li><a href="./system-tala-html-nn-memory-runtime.html" data-target-node-id="system-tala-html-nn-memory-runtime" data-relationship-type="depends-on" data-relationship-weight="0.90" data-navigation-role="memory-runtime" data-authority-required="none">Tala HTML-NN Memory Runtime</a></li>
      <li><a href="./system-html-nn-authoring-guide.html" data-target-node-id="system-html-nn-authoring-guide" data-relationship-type="bounded-by" data-relationship-weight="0.85" data-navigation-role="authoring-guide" data-authority-required="none">HTML-NN Authoring Guide</a></li>
    </ul>
  </nav>

  <script type="application/tala-director+json"
          id="director-reference-operator-control-plane"
          data-script-kind="reference"
          data-side-effects="none">
{
  "when": {
    "intent": "explain_tala_control_plane"
  },
  "reference": [
    { "type": "html-nn", "node_id": "system-tala-operator-control-plane", "required": true },
    { "type": "html-nn", "node_id": "system-tala-configurable-provider-surface", "required": false },
    { "type": "html-nn", "node_id": "system-tala-html-nn-memory-runtime", "required": false }
  ],
  "then": {
    "instruction": "Explain Tala as an explicit operator-configured agent workbench. Do not describe provider, model, OAuth, guardrail, workflow, or agent setup as automatic unless referring only to system dependency provisioning."
  }
}
  </script>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "title": "Tala Operator Control Plane",
  "description": "Documents Tala's manual operator-owned provider, OAuth, MCP, guardrail, workflow, agent, and admin builder surfaces.",
  "tags": [
    "documentation",
    "operator-control-plane",
    "providers",
    "oauth",
    "mcp",
    "guardrails",
    "workflows",
    "agents",
    "astro-emotional-engine",
    "chrono-astrology",
    "swiss-ephemeris",
    "user-profile",
    "contacts",
    "blockchain",
    "firewall",
    "p2p",
    "html-nn-memory"
  ],
  "capabilities_documented": [
    "manual_provider_configuration",
    "oauth_auth_configuration",
    "mcp_registry",
    "guardrail_builder",
    "workflow_builder",
    "agent_builder",
    "astro_emotional_engine",
    "user_profile_astro",
    "agent_astro",
    "predictive_astro",
    "user_agent_synastry",
    "chrono_astrology_transits",
    "bounded_style_modulation",
    "user_profile",
    "contacts_directory",
    "blockchain_firewall",
    "proof_of_authority_chain_setup",
    "p2p_handshake",
    "settings_as_html_nn_memory"
  ],
  "non_goals": [
    "automatic_model_selection",
    "hosted_portal_onboarding",
    "hidden_provider_recommendation",
    "unredacted_secret_exposure"
  ]
}
  </script>
</article>
</body>
</html>

```

# File: `system-tala-install-packaging-runtime.html`

## Overview

Defines rules for interacting with package managers (pip, uv, npm) and installation tasks.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-install-packaging-runtime">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Install, Packaging, and Runtime Setup</title>
</head>
<body>
<article
  data-node-id="system-tala-install-packaging-runtime"
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
  data-weight-governance-importance="0.88"
  data-governance-status="context-only"
  data-doc-target="tala.install.packaging.runtime"
  data-source-files="install-windows.ps1;install-macos.sh;install-linux.sh;scripts/first_run.py;scripts/doctor.ps1;scripts/doctor.sh;scripts/start-tala.ps1;scripts/start-tala.sh;electron/main.js;package.json;README.md;INSTALL.md;RELEASE_CHECKLIST.md"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-install-packaging-runtime"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Install, Packaging, and Runtime Setup</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-install-packaging-runtime</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Documents Tala's cross-platform install scripts, dependency detection, first-run bootstrap, packaged user-data layout, self-test lane, and release verification posture.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala must run on any supported system without assuming the machine is already a developer workstation. Platform installers detect and provision missing system dependencies, then the shared first-run bootstrap prepares local runtime state. Automatic setup is appropriate for machine readiness: Node.js, Python tooling, Python runtime, PostgreSQL, project dependencies, local directories, database schema, and validation tests. Automatic setup is not appropriate for user-owned provider/model/auth choices.</p>
    <p>The install path separates system dependency provisioning from operator configuration. Installers may install Node.js, uv, Python 3.11, and PostgreSQL. First-run may create <code>.env</code>, <code>.venv</code>, <code>vault/</code>, <code>nodes/</code>, <code>workflows/</code>, <code>data/</code>, and <code>scratch/</code>. The operator still owns provider endpoints, model names, OAuth credentials, MCP servers, guardrail policy, workflow definitions, agent bindings, user profile content, contacts, firewall trust policy, and chain/VPS connection choices.</p>
  </section>

  <section id="platform-installers">
    <h2>Platform Installers</h2>
    <ul>
      <li><code>install-windows.ps1</code>: explicitly enforces Administrator privileges to prevent silent installation failures, runs Doctor, provisions Node.js, uv, and PostgreSQL with <code>winget</code> (dynamically targeting the system drive for PostgreSQL), installs Python 3.11 through uv when Python is missing, bootstraps PostgreSQL, then runs first-run setup and self-test.</li>
      <li><code>install-macos.sh</code>: runs Doctor, prompts before installing Homebrew if missing, provisions Node.js, uv, and PostgreSQL with <code>brew</code>, installs Python 3.11 through uv when Python is missing, starts PostgreSQL, then runs first-run setup and self-test.</li>
      <li><code>install-linux.sh</code>: supports Debian/Ubuntu through <code>apt-get</code> and Void Linux through <code>xbps-install</code>, provisions Node.js and PostgreSQL, prompts before running the official uv installer when uv is missing, installs Python 3.11 through uv when Python is missing, starts PostgreSQL when possible, then runs first-run setup and self-test.</li>
    </ul>
  </section>

  <section id="doctor-diagnostics">
    <h2>Doctor Diagnostics</h2>
    <p>Doctor scripts are preflight and troubleshooting tools. They check operating system, admin/sudo posture, Node.js, uv, Python runtime, PostgreSQL client, <code>.env</code>, and key port availability. Port <code>5432</code> in use is normally PostgreSQL. Ports <code>8000</code> and <code>8765</code> in use are critical because they can block Tala backend or WebSocket startup.</p>
    <ul>
      <li><code>scripts/doctor.ps1</code> handles Windows diagnostics.</li>
      <li><code>scripts/doctor.sh</code> handles macOS and Linux diagnostics.</li>
      <li>Doctor exit code <code>0</code> means healthy, <code>1</code> means missing dependencies, and <code>2</code> means critical runtime blockers.</li>
    </ul>
  </section>

  <section id="first-run-bootstrap">
    <h2>First-Run Bootstrap</h2>
    <p><code>scripts/first_run.py</code> is the shared bootstrap path. It resolves install root, user-data root, virtualenv path, and environment-file path from environment variables when present, allowing the same setup logic to work for source installs and packaged Electron installs.</p>
    <ul>
      <li><code>TALA_INSTALL_ROOT</code>: directory containing source/package application files.</li>
      <li><code>TALA_USER_DATA_ROOT</code>: writable runtime state directory.</li>
      <li><code>TALA_VENV_DIR</code>: Python virtual environment directory.</li>
      <li><code>TALA_ENV_PATH</code>: active <code>.env</code> file loaded by Python config.</li>
    </ul>
    <p>First-run creates runtime directories, creates a default <code>.env</code> only when missing, installs Node project dependencies unless skipped, creates or repairs the Python virtualenv, installs Python requirements, initializes the database schema when PostgreSQL is reachable, and can run Tala self-test.</p>
  </section>

  <section id="packaged-runtime">
    <h2>Packaged Runtime</h2>
    <p>Electron packages use <code>electron-builder</code>. Runtime state is not baked into the app bundle. In packaged mode, Electron points first-run state at the app's per-user <code>userData</code> directory and passes <code>TALA_ENV_PATH</code> to the Python harness. This keeps local secrets, virtualenv files, workflows, vault contents, data files, and scratch files outside the application installation directory.</p>
    <ul>
      <li><code>npm run pack</code>: creates an unpacked app directory for smoke testing.</li>
      <li><code>npm run package</code>: builds installer/archive targets for the current OS.</li>
      <li><code>npm run package:win</code>: Windows NSIS and zip targets.</li>
      <li><code>npm run package:mac</code>: macOS dmg and zip targets.</li>
      <li><code>npm run package:linux</code>: Linux AppImage, deb, and tar.gz targets.</li>
    </ul>
  </section>

  <section id="auto-updater">
    <h2>Auto-Updater</h2>
    <p>The auto-updater automatically downloads the latest packaged release and extracts it over the existing installation directory. Due to platform-specific file locking behavior (especially on Windows), the update extraction scripts are spawned in a detached, visible console window and implement a robust retry loop. This gives the backend Node and Python processes sufficient time to terminate cleanly and release file locks before the <code>Expand-Archive</code> or equivalent extraction command overwrites the local application files.</p>
  </section>

  <section id="self-test-lane">
    <h2>Self-Test Lane</h2>
    <p>Tala has a repeatable self-test lane for dependency health and code validation. It can be run from the terminal or chat, and it is intended to support Tala self-testing and future self-repair loops.</p>
    <ul>
      <li><code>npm run self-test</code>: terminal entrypoint.</li>
      <li><code>/self-test</code>: chat command entrypoint.</li>
      <li><code>/self-test --no-audit</code>: faster inner loop without npm audit.</li>
      <li>Checks include Python dependency imports, Python syntax compile, renderer syntax check, Electron syntax check, Python tests, Jest, and <code>npm audit --audit-level=moderate</code>.</li>
    </ul>
  </section>

  <section id="release-verification">
    <h2>Release Verification</h2>
    <p><code>RELEASE_CHECKLIST.md</code> defines the clean-machine verification plan. A release is not mature until installers are tested on fresh Windows, macOS, and Linux systems; first-run is idempotent; packaged first launch creates userData runtime state; self-test passes; and package artifacts launch cleanly.</p>
    <p>The important boundary is this: installers and first-run may provision machine dependencies, but they must not overwrite user configuration or silently choose providers, models, OAuth credentials, guardrails, workflows, agents, user profile data, contacts, firewall trust permissions, or chain/VPS connection mode.</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="./system-tala-agent-architecture.html" data-target-node-id="system-tala-agent-architecture" data-relationship-type="supports" data-relationship-weight="0.88" data-navigation-role="architecture-overview" data-authority-required="none">Tala Agent Architecture</a></li>
      <li><a href="./system-tala-operator-control-plane.html" data-target-node-id="system-tala-operator-control-plane" data-relationship-type="supports" data-relationship-weight="0.92" data-navigation-role="operator-control-plane" data-authority-required="none">Tala Operator Control Plane</a></li>
      <li><a href="./system-tala-configurable-provider-surface.html" data-target-node-id="system-tala-configurable-provider-surface" data-relationship-type="bounded-by" data-relationship-weight="0.80" data-navigation-role="manual-configuration-boundary" data-authority-required="none">Tala Configurable Provider Surface</a></li>
      <li><a href="./system-self-improvement-loop.html" data-target-node-id="system-self-improvement-loop" data-relationship-type="supports" data-relationship-weight="0.72" data-navigation-role="self-testing" data-authority-required="none">Tala Self-Improvement Loop</a></li>
    </ul>
  </nav>

  <script type="application/tala-director+json"
          id="director-reference-install-packaging-runtime"
          data-script-kind="reference"
          data-side-effects="none">
{
  "when": {
    "intent": "explain_tala_install_or_packaging"
  },
  "reference": [
    { "type": "html-nn", "node_id": "system-tala-install-packaging-runtime", "required": true },
    { "type": "html-nn", "node_id": "system-tala-operator-control-plane", "required": false }
  ],
  "then": {
    "instruction": "Explain that dependency provisioning is automatic where possible, but provider, model, OAuth, MCP, guardrail, workflow, and agent choices remain operator-owned."
  }
}
  </script>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "title": "Tala Install, Packaging, and Runtime Setup",
  "description": "Documents cross-platform dependency provisioning, first-run bootstrap, packaged runtime userData, self-test, and release verification boundaries.",
  "tags": [
    "documentation",
    "install",
    "packaging",
    "first-run",
    "self-test",
    "dependency-detection",
    "operator-owned-configuration",
    "auto-updater"
  ],
  "automatic_setup_allowed_for": [
    "nodejs",
    "uv",
    "python_runtime",
    "postgresql",
    "project_dependencies",
    "runtime_directories",
    "database_schema",
    "self_test"
  ],
  "automatic_setup_not_allowed_for": [
    "provider_choice",
    "model_choice",
    "oauth_credentials",
    "mcp_server_choice",
    "guardrail_policy",
    "workflow_design",
    "agent_persona"
    ,
    "user_profile",
    "contacts",
    "firewall_trust_policy",
    "chain_connection_mode",
    "vps_peer_endpoint"
  ]
}
  </script>
</article>
</body>
</html>

```

# File: `system-tala-embedded-inference-runtime.html`

## Overview

Instructions for fallback behavior when using the offline embedded inference engine.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-tala-embedded-inference-runtime">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-created" content="2026-06-08T00:00:00Z">
  <meta name="tala-author" content="codex-review">
  <title>Neural Node: Tala Embedded Inference Runtime</title>
</head>
<body>
<article
  data-node-id="system-tala-embedded-inference-runtime"
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
  data-weight-governance-importance="0.90"
  data-governance-status="context-only"
  data-doc-target="tala.embedded-inference"
  data-source-files="python/inference/embedded_engine.py;python/harness/server.py;python/harness/db.py;python/requirements.txt;renderer/renderer.js;scripts/first_run.py;package.json"
  data-source-fingerprint="sha256:manual-review-2026-06-08-tala-embedded-inference-runtime"
  data-generated-at="2026-06-08T00:00:00Z"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">Tala Embedded Inference Runtime</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-tala-embedded-inference-runtime</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Schema Version</dt>
      <dd><code>neural-node/v2</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>0.00</code></dd>
      <dt>Memory Description</dt>
      <dd>Documents Tala's built-in embedded inference provider, dependency lifecycle, model cache lifecycle, status/download RPCs, and operator-facing boundaries.</dd>
      <dt>Created</dt>
      <dd><time datetime="2026-06-08T00:00:00Z">2026-06-08</time></dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala includes a built-in embedded inference provider so the desktop app can have a local last-resort model path. This provider is available in code and persisted as <code>embedded-builtin</code>, but it is not the same as automatic model selection. Tala remains operator-configured: the user chooses the provider and model they want, and the embedded provider is one explicit option in that ecosystem.</p>
    <p>The embedded model is <code>Qwen/Qwen2.5-0.5B-Instruct</code>. Python dependencies are installed during first-run through <code>python/requirements.txt</code>, while model weights are managed by cache/status/download lifecycle code. First-run prepares the runtime; it does not truthfully guarantee model weights are already downloaded unless the cache says ready.</p>
  </section>

  <section id="provider-record">
    <h2>Provider Record</h2>
    <ul>
      <li><code>python/harness/db.py</code> seeds <code>inference_providers</code> with provider ID <code>embedded-builtin</code>.</li>
      <li>The provider type is <code>embedded</code>, the model is <code>Qwen/Qwen2.5-0.5B-Instruct</code>, and the record is locked so it cannot be casually deleted.</li>
      <li>The provider should be displayed as explicit local infrastructure, not as a hidden automatic replacement for the user's chosen provider.</li>
    </ul>
  </section>

  <section id="dependency-lifecycle">
    <h2>Dependency Lifecycle</h2>
    <ul>
      <li><code>scripts/first_run.py</code> installs Python dependencies from <code>python/requirements.txt</code>.</li>
      <li><code>python/requirements.txt</code> includes <code>torch</code>, <code>transformers</code>, <code>accelerate</code>, <code>sentencepiece</code>, and <code>huggingface_hub</code> support for embedded inference.</li>
      <li>Platform installers provision Python, Node.js, PostgreSQL, and <code>uv</code> where possible, then call first-run.</li>
      <li>Packaged Electron startup can invoke first-run using user-data paths when local runtime assets are missing.</li>
    </ul>
  </section>

  <section id="model-cache-lifecycle">
    <h2>Model Cache Lifecycle</h2>
    <ul>
      <li><code>python/inference/embedded_engine.py</code> checks whether the model is cached before generation.</li>
      <li><code>inference.get_embedded_status</code> reports cache/download status, progress, and error state.</li>
      <li><code>inference.trigger_download</code> starts a background model download.</li>
      <li>Harness startup initializes status from cache and can start a background download when the model is missing.</li>
      <li>The renderer polls embedded status so the UI can show whether embedded inference is ready, downloading, missing, or failed.</li>
    </ul>
  </section>

  <section id="generation-path">
    <h2>Generation Path</h2>
    <ul>
      <li><code>inference.generate</code> can route to the embedded provider when the selected provider or priority chain points to <code>embedded-builtin</code>.</li>
      <li>Generation calls <code>generate_stream</code> from <code>python/inference/embedded_engine.py</code>.</li>
      <li>If the model is not cached, the backend must report the unavailable state instead of pretending the embedded model is ready.</li>
      <li>Streaming output remains subject to the same guardrail, session, and operator configuration expectations as remote providers.</li>
    </ul>
  </section>

  <section id="repair-rules">
    <h2>Repair Rules</h2>
    <ol>
      <li>If the embedded model name changes, update the database seed, embedded engine, provider UI copy, package docs, and this node.</li>
      <li>If first-run begins downloading model weights, update this node and install docs to say model download is part of setup.</li>
      <li>If dependencies change, update <code>python/requirements.txt</code>, first-run validation, self-test imports, and this node.</li>
      <li>If provider routing changes, preserve the manual provider/model contract and update configurable provider docs.</li>
      <li>If cache detection changes, validate <code>inference.get_embedded_status</code> and <code>inference.trigger_download</code>.</li>
    </ol>
  </section>

  <section id="relationships">
    <h2>Relationships</h2>
    <ul>
      <li><a href="system-tala-configurable-provider-surface.html" data-relationship="implements-option-for">Configurable Provider Surface</a></li>
      <li><a href="system-tala-install-packaging-runtime.html" data-relationship="depends-on">Install and Packaging Runtime</a></li>
      <li><a href="system-tala-rpc-surface-reference.html" data-relationship="exposes">RPC Surface Reference</a></li>
      <li><a href="system-tala-database-schema-reference.html" data-relationship="persists-through">Database Schema Reference</a></li>
    </ul>
  </section>

  <script type="application/json" id="node-state">
  {
    "schema": "tala-node-state/v1",
    "node_id": "system-tala-embedded-inference-runtime",
    "status": "active",
    "provider": {
      "id": "embedded-builtin",
      "type": "embedded",
      "model": "Qwen/Qwen2.5-0.5B-Instruct",
      "locked": true,
      "selection_contract": "operator-explicit"
    },
    "lifecycle": {
      "dependencies_installed_by_first_run": true,
      "model_weights_forced_by_first_run": false,
      "model_weights_managed_by_cache_status_and_download_rpc": true,
      "startup_cache_probe": true
    },
    "rpc_methods": ["inference.get_embedded_status", "inference.trigger_download", "inference.generate"]
  }
  </script>
</article>
</body>
</html>

```

# File: `system-multimodality-and-embedded-safety.html`

## Overview

System prompt enforcing safety constraints and defining multimodal input handling logic (vision, audio).

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-multimodality-and-embedded-safety">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-author" content="tala-system">
  <title>Neural Node: Multimodality and Embedded Safety</title>
</head>
<body>
<article
  data-node-id="system-multimodality-and-embedded-safety"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-governance-status="context-only"
  data-doc-target="multimodality-embedded-safety"
  data-source="local"
  data-trust-tier="system"
  data-privacy="public">

  <header id="node-identity">
    <h1 id="node-title">Multimodality and Embedded Safety</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-multimodality-and-embedded-safety</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Memory Description</dt>
      <dd>Explains Tala's Desktop-Native Multimodality capabilities and Headless Safety constraints.</dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala has Desktop-Native Multimodal capabilities, enabling her to "see" and "hear" the environment when running in a desktop OS context. These features are available via internal JSON-RPC bridges (<code>multimodal.capture_screen</code> and <code>multimodal.transcribe_voice</code>).</p>
    <ul>
      <li><strong>Screen Context</strong>: Powered by <code>pyautogui</code> and <code>Pillow</code>, Tala can capture the active screen window as a Base64 image payload to pass to Vision Models (e.g., LLaVA).</li>
      <li><strong>Voice Commands</strong>: Powered by a local <code>openai-whisper</code> model, users can speak to Tala. Transcriptions happen entirely offline.</li>
      <li><strong>Browser Use</strong>: Extending the Electron browser integration, Tala can broadcast DOM and JS execution requests to her built-in webview.</li>
    </ul>

    <h2>Embedded System Safety (Headless Mode)</h2>
    <p>Because Tala can also be deployed as an "embedded system" (e.g., headless servers, TUI interfaces, or IoT hardware without a Desktop environment), a safety constraint exists: <code>TALA_HEADLESS</code>. When this environment variable is true, all multimodal tools dynamically block execution and return an explicit error explaining that the runtime context prohibits UI/Audio access. This ensures Tala does not crash when deployed in restricted, embedded environments.</p>
  </section>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "title": "Multimodality and Embedded Safety",
  "description": "Rules and architectural specs for Tala's desktop multimodality tools and headless system safety toggles.",
  "tags": [
    "html-nn",
    "architecture",
    "multimodality",
    "vision",
    "voice",
    "embedded",
    "headless",
    "safety"
  ]
}
  </script>
</article>
</body>
</html>

```

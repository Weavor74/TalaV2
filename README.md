# Tala Cognitive Engine Room

Tala is an offline-first, desktop-native AI orchestration shell built with Electron and a Python backend. It serves as a secure, distributed, and highly-capable agentic ecosystem (conceptually similar to self-improving agents like Hermes, but fortified with enterprise-grade data persistence and peer-to-peer networking).

This repository is intentionally committed as a blank slate: generated node files, local databases, snapshots, caches, and runtime blockchain state are excluded from source control.

## Core Capabilities

- **P2P Mesh Networking & Remote Orchestration:** Tala integrates a WebRTC/libp2p mesh network (`aiortc`), enabling secure peer-to-peer JSON-RPC over DataChannels. This allows agents to seamlessly interact, orchestrate remote services, or perform secure remote desktop operations across distributed nodes.
- **Tala Proof-of-Authority (PoA) Blockchain:** A localized blockchain ledger tracks trusted nodes, verifies peer identity during mesh orchestration, anchors distilled marketplace skills, and manages the distributed token economy for API billing and AI token balances.
- **HTML Neural Nodes (HTML-NN):** Tala's primary architecture for memory, logic, and self-documentation. These are human-readable HTML documents embedded with machine-readable semantic metadata and execution weights, allowing the AI to seamlessly parse, retrieve, and execute complex workflows or contextual data.
- **Autonomous Skill Distillation:** Tala features an autonomous learning loop (`SkillDistiller`) that analyzes successful workflows and distills them into reusable YAML/Python plugins. These skills are automatically packaged as marketplace-ready manifests that can be anchored to the Tala blockchain for network distribution.
- **Enterprise-Grade Memory (Mem0 + PostgreSQL):** Tala utilizes semantic vector memory (`mem0`) for fuzzy matching and deep historical context across sessions. Unlike standard agents, Tala wraps this in a strict read-only adapter, enforcing that all durable state (nodes, ledger, vault) is authoritatively and securely managed by a local PostgreSQL database.
- **Astro Emotion Engine:** A dynamic personality and emotional state engine that drives contextual persona variance. It manages agent profiles and stateful emotional responses, ensuring highly dynamic, human-like interactions across different agents and users.
- **Advanced Visual Builders:** The Electron interface provides interactive visual builders for zero-code orchestration:
  - **Agent Builder:** Create and refine complex agent personas with specific capabilities.
  - **Guardrail Builder:** Architect strict, rule-based safety, output, and behavior constraints.
  - **Workflow Builder:** Design multi-step, multi-agent workflows and node graphs via an intuitive visual editor.
- **Model Context Protocol (MCP) Integration:** Tala provides full support for the official MCP Python SDK, dynamically mapping and invoking external capabilities across both `stdio` (local subprocess) and `sse` (remote HTTP) transports.
- **Bring-Your-Own-Everything (BYOE):** Fully abstracted access supporting multiple embedded or remote providers for inference (vLLM, OpenAI, Anthropic, OpenRouter), authentication, and storage endpoints, orchestrated via the Python backend's configuration.

## Architecture Documents

Tala's primary architecture docs are HTML Neural Nodes in `nodes/`. These are human-readable documents and first-class Tala memory files, so the app can parse, retrieve, and activate them as internal context.

- `nodes/system-tala-agent-architecture.html` — desktop, backend, SDK, gateway, database, and HTML-NN runtime overview.
- `nodes/system-tala-configurable-provider-surface.html` — MCP, auth, endpoints, provider tables, RPC methods, and gateway/plugin boundaries.
- `nodes/system-tala-html-nn-memory-runtime.html` — HTML-NN primary memory, activation, relationship propagation, workflow auto-evaluation, and memory indexing.
- `nodes/system-tala-operator-control-plane.html` — operator-owned provider/model/OAuth/MCP setup, user profile, contacts, blockchain trust, guardrail builder, workflow builder, agent builder, and admin surfaces.
- `nodes/system-tala-install-packaging-runtime.html` — dependency detection, cross-platform installers, first-run setup, packaged runtime layout, self-test, and release verification.

- `nodes/system-tala-self-updating-documentation-contract.html` - mandatory contract for updating Tala's HTML-NN repair docs whenever code, schema, UI, provider, workflow, guardrail, astro, packaging, or runtime behavior changes.
- `nodes/system-tala-rpc-surface-reference.html` - JSON-RPC handler families, backend control surface, and self-repair rules for renderer/harness integration.
- `nodes/system-tala-database-schema-reference.html` - SQLite persistence map for nodes, providers, agents, guardrails, profiles, contacts, tasks, memory, and research state.
- `nodes/system-tala-embedded-inference-runtime.html` - embedded provider lifecycle, model cache/download behavior, dependency setup, and operator-facing inference boundaries.

Supporting schema docs:

- `nodes/system-neural-node-v2-schema.html`
- `nodes/system-html-nn-authoring-guide.html`
- `nodes/system-program-documentation-generator.html`

## Getting Started

Install dependencies and bootstrap the local environment with the platform installer:

- Windows: `.\install-windows.ps1`
- macOS: `./install-macos.sh`
- Linux (Debian/Ubuntu or Void): `./install-linux.sh`

The installers detect and provision missing system dependencies before bootstrap:

- Windows: installs Node.js, `uv`, and PostgreSQL with `winget`; installs Python 3.11 through `uv` if Python is missing.
- macOS: installs Homebrew if approved, then Node.js, `uv`, and PostgreSQL; installs Python 3.11 through `uv` if Python is missing.
- Linux: installs Node.js and PostgreSQL with `apt-get` or `xbps-install`; installs `uv` and Python 3.11 when missing.

All installers call `scripts/first_run.py`, which creates the local `.env`, prepares runtime directories, installs project Node/Python dependencies, initializes the database schema, and can run Tala's self-test.

After installation, launch Tala with:

- Windows: `.\scripts\start-tala.ps1`
- macOS/Linux: `./scripts/start-tala.sh`

For diagnostics, run the platform doctor script in `scripts/`. For packaged desktop builds, use:

- Directory pack: `npm run pack`
- Current OS installer: `npm run package`
- Platform targets: `npm run package:win`, `npm run package:mac`, or `npm run package:linux`

## Self Testing

Tala has a repeatable self-test lane for dependency health and code validation:

- From the terminal: `npm run self-test`
- From Tala chat: `/self-test`
- Fast inner loop without npm audit: `/self-test --no-audit`

The suite checks Python dependency imports, Python/JS syntax, Python tests under `tests/` excluding benchmarks, Jest with `--passWithNoTests`, and `npm audit --audit-level=moderate`.

## Permanent VPS Peer

To keep an always-on Tala peer for P2P handshakes, chat, and peer discovery, run the VPS mode. It starts the Python harness and the public blockchain discovery node:

- Guide: `docs/vps-peer.md`
- Start script: `scripts/start-tala-vps.sh`
- systemd template: `deploy/tala-vps.service`

Desktop clients choose `Connect to Tala VPS` after saving the user profile, or stay local-only.
Peer HTML-NN references can be written as `@user323/financial` when the peer is in Contacts and the firewall allows that node name or collection name.

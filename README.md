# Tala Cognitive Engine Room

Tala is an offline-first, desktop-native AI orchestration shell built with Electron and a Python backend. It is best understood as a **governed, local-first cognitive operating environment**: an agent runtime in which memory, tools, workflows, identity, permissions, documentation, multi-agent topology, provenance, and learned behavior are represented through one common, machine-readable substrate. (Conceptually related to self-improving agents like Hermes, but fortified with authoritative local data persistence, governance, and peer-to-peer networking.)

This repository is intentionally committed as a blank slate: generated node files, local databases, snapshots, caches, and runtime blockchain state are excluded from source control.

## Core Capabilities

- **Multi-Agent Orchestration — Crews, Swarms & Teams:** Tala runs many agents together under two deliberately different control shapes. **Crews** are *deterministic* organizations: members, roles, a reporting hierarchy, task routing (`crew.assign`) and escalation up the chain. A crew can be **run as a team** (`crew.run`) — every member works the same goal *at once*, and when the crew has a manager that manager synthesizes the members' answers into one. **Swarms** are *emergent* graphs: named DAG topologies with fan-out, join barriers, and gated edges, executed with real parallelism. Both are definable in the UI (the Agent Builder's **Teams** and **Topologies** tabs) and run/monitored live.

- **P2P Mesh Networking & Remote Orchestration:** Tala integrates a WebRTC mesh network (`aiortc`), enabling secure peer-to-peer JSON-RPC over DataChannels. This allows *independent* Tala installations to interact, orchestrate remote services, or perform secure remote desktop operations across distributed nodes.

- **Immutable Provenance Ledger & Token Economy:** A localized Proof-of-Authority (PoA) blockchain acts first as an **immutable provenance/audit ledger** — it anchors node provenance, tracks trusted peer identities used during mesh orchestration, and records skill anchors — and second as the settlement layer for a dual-mode token economy built on top of it:
  - **Ghost Tokens (local mode):** When set to *Stay Local*, all interactions use ghost tokens — an unlimited internal accounting unit that never touches the shared chain. Every call is still logged to the immutable local `token_ledger` for audit.
  - **Real Tokens (connected mode):** When connected to a peer network (VPS mode), cross-node inference calls that consume another peer's resources settle on-chain as signed payment transactions, minus a configurable house fee. Insufficient real-token balance raises a hard gate before inference proceeds.

  The economy, marketplace, and P2P settlement are *optional* applications; the local ledger's provenance/audit role does not require a user to think about wallets or tokens.

- **HTML Neural Nodes (HTML-NN):** Tala's primary substrate for memory, logic, and self-documentation — human-readable HTML documents carrying machine-readable semantic metadata, typed relationships, authority/trust/privacy labels, provenance, and execution weights. One graph serves at once as memory, operational map, documentation, skill representation, workflow definition, and self-model, so the AI can parse, retrieve, execute, and maintain it without retraining the base model.

- **Autonomous Skill Distillation & Ecosystem Interop:** An autonomous learning loop (`SkillDistiller`) analyzes successful workflows and distills them into reusable skills, packaged as marketplace-ready manifests that can be anchored to the Tala ledger for network distribution. Tala also imports the open `agentskills.io` **`SKILL.md`** format (`import_skill_md`), wrapping community skills as governed HTML-NN nodes with provenance, trust, and relationships.

- **Governance & Security:** Governance is architectural rather than bolted on: a unified capability/grant model, guardrails that govern the *harness* (not just model text), encrypted credentials, network firewalling, promptware redaction, plugin/supply-chain validation, signed skills, a peer trust registry, and end-to-end encryption.

- **Self-Knowledge & Self-Operation:** Tala maintains a machine-readable model of its own architecture and UI — `describe_architecture`, an architecture contract, documentation RPCs, AST-derived program nodes, and drift-guarded UI-view nodes — so it (and any agent working on it) can inspect real services, tools, RPC flows, and surfaces before changing them.

- **Enterprise-Grade Memory (PostgreSQL + optional Mem0):** A local **PostgreSQL** database is the sole durable operational authority for all state (nodes, ledger, vault); ChromaDB, Postgres full-text search, and mem0 are *derived, rebuildable* indexes. The `Mem0ReadOnlyAdapter` enforces a hard invariant — mem0 writes are blocked at the adapter level; install the `[mem0]` extra to enable read-only semantic recall.

- **Astro Emotion Engine:** A dynamic personality and emotional-state engine that drives contextual persona variance across different agents and users.

- **Advanced Visual Builders:** The interface provides interactive builders for zero-code orchestration:
  - **Agent Builder:** create agent personas, define **Teams** (crews) and run them, and design **Swarm Topologies**.
  - **Guardrail Builder:** architect strict, rule-based safety, output, and behavior constraints.
  - **Workflow Builder:** design multi-step tool/script/MCP workflows and node graphs visually.

- **Messaging Channels:** Inbound messages from **Telegram** and **Discord** flow through the full agent pipeline (guardrails, retrieval, tools). Adapters are poll-based and hot-reloadable; additional platforms are additive.

- **Model Context Protocol (MCP) Integration:** Full support for the official MCP Python SDK, dynamically discovering and invoking external capabilities across both `stdio` (local subprocess) and `sse` (remote HTTP) transports.

- **Bring-Your-Own-Everything (BYOE):** The only *pre-configured* inference provider is the **embedded local model** (transformers, `Qwen/Qwen2.5-0.5B-Instruct`), which runs with no internet. Every other model is reached through a generic **OpenAI-compatible provider** interface that the end user configures and selects — point it at a local server (e.g. **Ollama**, **vLLM**, **LM Studio**) or any remote OpenAI-compatible API (e.g. **OpenAI**, **Anthropic**, **Gemini**, **DeepSeek**, **Kimi**) by supplying a base URL, model, and key. Provider configuration is runtime data; native function calling is normalized at the boundary, and provider failover preserves an offline-first path rather than making cloud access a hidden prerequisite. Storage and authentication endpoints are similarly pluggable.

## Architecture Documents

Tala's primary architecture docs are HTML Neural Nodes in `nodes/`. These are human-readable documents and first-class Tala memory files, so the app can parse, retrieve, and activate them as internal context.

- `nodes/system-tala-agent-architecture.html` — desktop, backend, SDK, gateway, database, and HTML-NN runtime overview.
- `nodes/system-tala-configurable-provider-surface.html` — MCP, auth, endpoints, provider tables, RPC methods, and gateway/plugin boundaries.
- `nodes/system-tala-html-nn-memory-runtime.html` — HTML-NN primary memory, activation, relationship propagation, workflow auto-evaluation, and memory indexing.
- `nodes/system-tala-operator-control-plane.html` — operator-owned provider/model/OAuth/MCP setup, user profile, contacts, blockchain trust, guardrail builder, workflow builder, agent builder, and admin surfaces.
- `nodes/system-tala-install-packaging-runtime.html` — dependency detection, cross-platform installers, first-run setup, packaged runtime layout, self-test, and release verification.
- `nodes/system-tala-self-updating-documentation-contract.html` — mandatory contract for updating Tala's HTML-NN repair docs whenever code, schema, UI, provider, workflow, guardrail, astro, packaging, or runtime behavior changes.
- `nodes/system-tala-rpc-surface-reference.html` — JSON-RPC handler families, backend control surface, and self-repair rules for renderer/harness integration.
- `nodes/system-tala-database-schema-reference.html` — PostgreSQL persistence map for nodes, providers, agents, guardrails, profiles, contacts, tasks, memory, and research state.
- `nodes/system-tala-embedded-inference-runtime.html` — embedded provider lifecycle, model cache/download behavior, dependency setup, and operator-facing inference boundaries.

Supporting schema docs:

- `nodes/system-neural-node-v2-schema.html`
- `nodes/system-html-nn-authoring-guide.html`
- `nodes/system-program-documentation-generator.html`

A competitive/architecture comparison against other agent interfaces lives at `docs/competitive-comparison.md`.

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

## Web UI

The Electron renderer also runs in a browser. It is the same `renderer/` directory, not a second implementation: Electron supplies `window.tala` through preload IPC, and `renderer/tala-web-shim.js` supplies the same object backed by the WebSocket the harness has always spoken.

```bash
npm run web
```

Then open `http://127.0.0.1:8767`. This starts the harness on its own — no Electron — with the browser UI switched on. The UI is off in every other launch path unless `TALA_WEB_UI=1` is set.

| Variable | Default | Meaning |
|---|---|---|
| `TALA_WEB_UI` | unset (off) | `1` to serve the UI |
| `TALA_WEB_HOST` | `127.0.0.1` | interface to bind |
| `TALA_WEB_PORT` | `8767` | 8765 is the harness, 8766 is MCP, 8332 is the chain, 8000 is vLLM |
| `TALA_WEB_TOKEN` | unset | required for any non-loopback bind |

The UI reaches **every** JSON-RPC handler the harness exposes, including shell execution, source patching, and vault access. Binding it anywhere other than loopback therefore requires `TALA_WEB_TOKEN`; without one the server logs why and declines to start rather than publishing a remote shell.

Two differences from the desktop app, both deliberate:

- `terminal:kill` is unavailable. The harness owns the shell rather than handing out per-session process handles, and the shim rejects the call by name instead of silently doing nothing.
- `vault:read` is not served over HTTP. Electron gated it on there being no outbound connections, a check a web server cannot honestly make; the `vault.read` JSON-RPC method applies the vault's own rules instead.

## Self Testing

Tala has a repeatable self-test lane for dependency health and code validation:

- From the terminal: `npm run self-test`
- From Tala chat: `/self-test`
- Fast inner loop without npm audit: `/self-test --no-audit`

The suite checks Python dependency imports, Python/JS syntax, Python tests under `tests/` (excluding benchmarks), Jest with `--passWithNoTests`, and `npm audit --audit-level=moderate`. CI runs the same lane on every push and pull request (with a provisioned Postgres service and a `tala_test` sibling database).

## Permanent VPS Peer

To keep an always-on Tala peer for P2P handshakes, chat, and peer discovery, run the VPS mode. It starts the Python harness and the public blockchain discovery node:

- Guide: `docs/vps-peer.md`
- Start script: `scripts/start-tala-vps.sh`
- systemd template: `deploy/tala-vps.service`

Desktop clients choose `Connect to Tala VPS` after saving the user profile, or stay local-only. Peer HTML-NN references can be written as `@user323/financial` when the peer is in Contacts and the firewall allows that node name or collection name.

# Tala Cognitive Engine Room

Tala is an offline-first, desktop-native AI orchestration shell built with Electron and a Python backend. It serves as a secure, distributed, and highly-capable agentic ecosystem (conceptually similar to self-improving agents like Hermes, but fortified with enterprise-grade data persistence and peer-to-peer networking).

This repository is intentionally committed as a blank slate: generated node files, local databases, snapshots, caches, and runtime blockchain state are excluded from source control.

## Core Capabilities

- **P2P Mesh Networking & Remote Orchestration:** Tala integrates a WebRTC mesh network (`aiortc`), enabling secure peer-to-peer JSON-RPC over DataChannels. This allows agents to seamlessly interact, orchestrate remote services, or perform secure remote desktop operations across distributed nodes.
- **Tala Proof-of-Authority (PoA) Blockchain:** A localized blockchain ledger tracks trusted nodes, verifies peer identity during mesh orchestration, and anchors distilled marketplace skills. Tala operates a dual-mode token economy:
  - **Ghost Tokens (local mode):** When the system is set to Stay Local, all interactions use ghost tokens — an unlimited internal accounting unit that never touches the blockchain. Every call is still logged to the immutable local `token_ledger` for audit purposes.
  - **Real Tokens (connected mode):** When connected to a peer network (VPS mode), cross-node inference calls that consume another peer's resources settle on-chain as signed payment transactions. The counterparty receives the payment minus a configurable house fee. Insufficient real-token balance raises a hard gate before inference proceeds.
- **HTML Neural Nodes (HTML-NN):** Tala's primary architecture for memory, logic, and self-documentation. These are human-readable HTML documents embedded with machine-readable semantic metadata and execution weights, allowing the AI to seamlessly parse, retrieve, and execute complex workflows or contextual data.
- **Autonomous Skill Distillation:** Tala features an autonomous learning loop (`SkillDistiller`) that analyzes successful workflows and distills them into reusable YAML/Python plugins. These skills are automatically packaged as marketplace-ready manifests that can be anchored to the Tala blockchain for network distribution.
- **Enterprise-Grade Memory (Mem0 + PostgreSQL):** Tala optionally integrates semantic vector memory (`mem0ai`) as a read-only fuzzy-search cache for deep historical context across sessions. The `Mem0ReadOnlyAdapter` enforces a hard architectural invariant: all durable state (nodes, ledger, vault) is authoritatively managed by a local PostgreSQL database; mem0 writes are blocked at the adapter level. Install the `[mem0]` extra to enable semantic recall.
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
- `nodes/system-tala-database-schema-reference.html` - PostgreSQL persistence map for nodes, providers, agents, guardrails, profiles, contacts, tasks, memory, and research state.
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

For diagnostics, run the platform doctor script in `scripts/`.

Distribution is the source zips (there is no separate installer format —
electron-builder was dropped 2026-09-23; installs run `npm install`/`uv sync`
on the target via the bundled install scripts):

- Build a release: `python scripts/build_bundles.py` (bumps the version from
  `CHANGELOG.md`'s `[Unreleased]` notes, builds the three platform zips,
  stamps their sha256 into `blockchain/public/version.json`)
- Publish it: `scripts/upload_vps.ps1`
- Prove it: `scripts/verify_release_upgrade.ps1 -BaselineZip <previous zip>`

## Local model stack (US-origin only)

Inference is local-first and uses US-origin models only (Meta Llama, OpenAI
gpt-oss, Google Gemma); no cloud provider is enabled by default. A **profile**
pins the stack for a machine. On the Windows dev PC (`kagura`: RTX 3050 6 GB):

| Role | Model | Served by |
|---|---|---|
| Main chat + tools | gpt-oss-20b Heretic `MXFP4_MOE` | `llama-server` (llama.cpp CUDA build) |
| Fallback | Llama 3.1 8B abliterated `Q4_K_M` | Ollama |
| Vision | `gemma3:4b` at `num_ctx` 8192 | Ollama |

The router picks vision for an image turn, main otherwise, and falls back on an
error or timeout. The 6 GB GPU holds one big model at a time, so the stack
swaps them (Ollama `keep_alive`, `llama-server` stop/start). Everything lives
on D: (`OLLAMA_MODELS=D:\ollama\models`).

```powershell
.\scripts\kagura-stack.ps1 -Check     # what is present
.\scripts\kagura-stack.ps1            # pull/fetch what is missing, then start Tala

python -m python.inference.profiles list         # profiles
python -m python.inference.profiles use nano     # switch (or set TALA_PROFILE)
```

Setup steps, the exact `llama-server` command, the Ollama pulls, `num_ctx`, the
profile config and how to switch are in [docs/inference-profiles.md](docs/inference-profiles.md).
LoRA is **not implemented** (the engine status API reports it).

## Web UI

The Electron renderer also runs in a browser. It is the same `renderer/`
directory, not a second implementation: Electron supplies `window.tala` through
preload IPC, and `renderer/tala-web-shim.js` supplies the same object backed by
the WebSocket the harness has always spoken.

```bash
npm run web
```

Then open `http://127.0.0.1:8767`. This starts the harness on its own — no
Electron — with the browser UI switched on. The UI is off in every other launch
path unless `TALA_WEB_UI=1` is set.

| Variable | Default | Meaning |
|---|---|---|
| `TALA_WEB_UI` | unset (off) | `1` to serve the UI |
| `TALA_WEB_HOST` | `127.0.0.1` | interface to bind |
| `TALA_WEB_PORT` | `8767` | 8765 is the harness, 8766 is MCP, 8000 is vLLM |
| `TALA_WEB_TOKEN` | unset | required for any non-loopback bind; also pins the harness socket token |
| `TALA_WS_ALLOWED_ORIGINS` | unset | extra browser origins allowed on 8765, comma-separated |

**The UI reaches all 247 JSON-RPC handlers**, including shell execution, source
patching and vault access. Binding it anywhere other than loopback therefore
requires `TALA_WEB_TOKEN`; without one the server logs why and declines to
start rather than publishing a remote shell.

### The harness socket (8765) is authenticated

The same 247 handlers sit behind the WebSocket on 8765, and until recently it
accepted every connection. Loopback is not a control here: **WebSocket
handshakes are exempt from CORS**, so any page the user visited could open a
socket to their own machine and drive the whole surface. Two gates now apply to
every handshake:

* **Origin** — a browser always sends it and page script cannot forge it. Only
  the web UI's own origin is accepted; add others with
  `TALA_WS_ALLOWED_ORIGINS`. `null` is refused, because that is what a
  sandboxed hostile iframe sends.
* **Token** — read from `TALA_WEB_TOKEN`, or generated on first use into
  `vault/.keys/ws_token.txt` (mode 0600, already excluded from git). Present it
  as `Authorization: Bearer <token>`, `X-Tala-Token`, or `?token=` — the query
  form exists because `new WebSocket()` cannot set headers.

Nothing needs configuring for the desktop app: Electron establishes the token
before it spawns the harness and sends it on connect.

**Federated peers must be told the token.** A remote Tala calling into this one
now needs it, so give the contact an endpoint of the form
`ws://<host>:8765?token=<their-token>`. A peer configured without one will be
refused, which is the intended direction of failure.

Two differences from the desktop app, both deliberate:

- `terminal:kill` is unavailable. The harness owns the shell rather than
  handing out per-session process handles, and the shim rejects the call by
  name instead of silently doing nothing.
- `vault:read` is not served over HTTP. Electron gated it on there being no
  outbound connections, a check a web server cannot honestly make; the
  `vault.read` JSON-RPC method applies the vault's own rules instead.

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

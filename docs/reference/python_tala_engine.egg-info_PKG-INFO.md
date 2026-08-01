# File: `PKG-INFO`

## Overview

This file contains generic configuration or data.

## File Contents

```
Metadata-Version: 2.4
Name: tala-engine
Version: 0.1.9
Summary: Tala Cognitive Engine Room — local AI orchestration shell
Author: Tala Project
Requires-Python: >=3.10
Description-Content-Type: text/markdown
Requires-Dist: websockets==16.0
Requires-Dist: asyncpg==0.31.0
Requires-Dist: markdown==3.10.2
Requires-Dist: PyYAML==6.0.3
Requires-Dist: tiktoken==0.13.0
Requires-Dist: python-dotenv==1.2.2
Requires-Dist: pydantic==2.13.4
Requires-Dist: openai==2.44.0
Requires-Dist: google-generativeai==0.8.6
Requires-Dist: transformers==5.12.1
Requires-Dist: accelerate==1.14.0
Requires-Dist: sentencepiece==0.2.1
Requires-Dist: textual==8.2.7
Requires-Dist: rich==15.0.0
Requires-Dist: mcp==1.28.0
Requires-Dist: click==8.4.2
Requires-Dist: httpx-sse==0.4.3
Requires-Dist: cryptography==49.0.0
Requires-Dist: chromadb==1.5.9
Requires-Dist: pyautogui==0.9.54
Requires-Dist: Pillow==12.2.0
Requires-Dist: mnemonic==0.21
Requires-Dist: aiortc==1.14.0
Requires-Dist: lxml==6.1.1
Provides-Extra: test
Requires-Dist: pytest==9.1.1; extra == "test"
Requires-Dist: pytest-asyncio==1.4.0; extra == "test"
Provides-Extra: voice
Requires-Dist: openai-whisper>=20231117; extra == "voice"
Requires-Dist: sounddevice>=0.4.6; extra == "voice"
Requires-Dist: numpy<2.5.0,>=1.26.0; extra == "voice"

# Tala Cognitive Engine Room

Tala is an offline-first, desktop-native AI orchestration shell built with Electron and a Python backend.

This repository is intentionally committed as a blank slate: generated node files, local databases, snapshots, caches, and runtime blockchain state are excluded from source control.

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

```

# Tala Cognitive Engine Room — Installation Guide

Tala uses a **Native Transparent Deployment** strategy. Instead of hiding bundled
binaries in a black box, Tala installs directly into your host machine using official
package managers (`winget`, `brew`, `apt`/`dnf`/`xbps`). This keeps your machine
clean, standardized, and fully inspectable.

**Nothing needs to be running before you start.** The installer provisions everything
from scratch — including PostgreSQL — on a bare machine. All you need is an internet
connection and the permissions described below.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Get the Code](#2-get-the-code)
3. [Windows Installation](#3-windows-installation)
4. [macOS Installation](#4-macos-installation)
5. [Linux Installation](#5-linux-installation)
6. [First Launch](#6-first-launch)
7. [Configuration Reference](#7-configuration-reference)
8. [Verify Your Installation](#8-verify-your-installation)
9. [Optional Features](#9-optional-features)
10. [Tala Doctor — Diagnostics](#10-tala-doctor--diagnostics)
11. [Uninstall](#11-uninstall)

---

## 1. Prerequisites

| Requirement | Windows | macOS | Linux |
|---|---|---|---|
| **Permissions** | PowerShell as **Administrator** | Terminal (no root required) | `sudo` or root |
| **Internet** | Required | Required | Required |
| `winget` | Built into Windows 11 | — | — |
| `Xcode Command Line Tools` | — | Installed automatically by Homebrew prompt | — |

> **No other software needs to be pre-installed.** Node.js, Python, and PostgreSQL are
> all provisioned by the installer if they are missing.

---

## 2. Get the Code

Clone or download the repository to a local directory of your choice:

```bash
git clone https://github.com/your-org/TalaV2.git
cd TalaV2
```

Or extract the zip archive and open a terminal in the resulting directory.

---

## 3. Windows Installation

### Step 1 — Open PowerShell as Administrator

Right-click the Start button → **Windows Terminal (Admin)** or **PowerShell (Admin)**.

> The installer requires Administrator rights to install PostgreSQL and to register
> its Windows service. You will see a UAC prompt.

### Step 2 — Allow Script Execution (if needed)

If you have never run PowerShell scripts before, set the execution policy once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Step 3 — Navigate to the repository

```powershell
cd C:\path\to\TalaV2
```

### Step 4 — Run the installer

```powershell
.\install-windows.ps1
```

The installer runs these steps automatically:

| Step | What Happens |
|---|---|
| **[1/6] Preflight** | `scripts\doctor.ps1` checks Node.js, Python, PostgreSQL, and ports. Reports what is missing. |
| **[2/6] Dependencies** | Installs any missing tools via `winget`: Node.js LTS, `uv` (Python package manager), PostgreSQL 17. |
| **[3/6] Database** | Starts the PostgreSQL Windows service, then creates the `tala` role and `tala` database. Prompts for the Postgres superuser password if peer auth is not set up. |
| **[4/6] Configuration** | Generates `.env` with local defaults if one does not already exist. Never overwrites an existing `.env`. |
| **[5/6] First-Run Setup** | Installs all Node and Python dependencies, then initializes the database schema. |
| **[6/6] Policy Check** | Confirms PowerShell execution policy is not `Restricted`. |

**When it finishes you will see:**
```
========================================================
 Installation Complete!
 Run Tala by executing: .\scripts\start-tala.ps1
========================================================
```

### pgvector on Windows (required for peer/VPS features)

The PostgreSQL packages that `winget` installs do **not** bundle the
[pgvector](https://github.com/pgvector/pgvector) extension. Without it the
embeddings table cannot be created, and every peer write to a connection node
is denied with "Memory context validation failed". `scripts\bootstrap-db.ps1`
and `scripts\doctor.ps1` both detect and report this.

To install it:

1. Download (or build, per the pgvector README) a release matching your
   PostgreSQL major version — prebuilt Windows binaries are attached to the
   releases at <https://github.com/pgvector/pgvector/releases>, or build with
   `nmake` following the README's Windows section.
2. Copy `vector.dll` into `C:\Program Files\PostgreSQL\17\lib\`, and
   `vector.control` plus the `vector--*.sql` files into
   `C:\Program Files\PostgreSQL\17\share\extension\`.
3. Re-run the bootstrap so the extension is enabled in both databases:
   ```powershell
   .\scripts\bootstrap-db.ps1
   ```

---

## 4. macOS Installation

### Step 1 — Open Terminal

Press `⌘ Space`, type `Terminal`, press Enter.

### Step 2 — Navigate to the repository

```bash
cd /path/to/TalaV2
```

### Step 3 — Run the installer

```bash
./install-macos.sh
```

The installer runs these steps automatically:

| Step | What Happens |
|---|---|
| **[1/6] Preflight** | `scripts/doctor.sh` checks Node.js, Python, PostgreSQL, and ports. |
| **[2/6] Dependencies** | If Homebrew is missing, **asks your permission** before downloading and running the official Homebrew installer. Then installs `node@20`, `uv`, and `postgresql@16` via `brew`. |
| **[3/6] Database** | Runs `brew services start postgresql@16`, then creates the `tala` role and `tala` database. |
| **[4/6] Configuration** | Generates `.env` with local defaults if one does not already exist. |
| **[5/6] First-Run Setup** | Installs all Node and Python dependencies, then initializes the database schema. |
| **[6/6] Final Checks** | Confirms everything looks healthy. |

---

## 5. Linux Installation

### Supported Distributions

| Distribution | Package Manager |
|---|---|
| Ubuntu 20.04+, Debian 11+ | `apt-get` |
| AlmaLinux 8+, RHEL 8+, Fedora 38+ | `dnf` |
| Void Linux | `xbps-install` |

### Step 1 — Open a terminal in the repository directory

```bash
cd /path/to/TalaV2
```

### Step 2 — Make the script executable (if needed)

```bash
chmod +x install-linux.sh
```

### Step 3 — Run the installer

```bash
./install-linux.sh
```

The installer runs these steps automatically:

| Step | What Happens |
|---|---|
| **[1/6] Preflight** | `scripts/doctor.sh` checks Node.js, Python, PostgreSQL, and ports. |
| **[2/6] Dependencies** | Installs `nodejs`, `npm`, and `postgresql` via your distro's package manager. For `uv`, **asks your permission** before running the official `astral.sh` installer script. |
| **[3/6] Database** | Enables and starts the PostgreSQL system service (`systemctl` or `sv`), then creates the `tala` role and `tala` database. |
| **[4/6] Configuration** | Generates `.env` with local defaults if one does not already exist. |
| **[5/6] First-Run Setup** | Installs all Node and Python dependencies, then initializes the database schema. |
| **[6/6] Final Checks** | Confirms everything looks healthy. |

---

## 6. First Launch

Once installation completes, start Tala with a single command.

**Windows:**
```powershell
.\scripts\start-tala.ps1
```

**macOS / Linux:**
```bash
./scripts/start-tala.sh
```

The start script:

1. Checks that `.env` and `.venv` exist. If either is missing, it runs first-run setup automatically before booting.
2. Starts the **Python cognitive backend** (`python.harness.server`) in the background.
3. Starts the **Electron desktop UI** (`npm start`).
4. When you close the Electron window, the script traps the exit signal and shuts down the backend cleanly.

> **Ports used:**
> - `5432` — PostgreSQL (database)
> - `8765` — Tala WebSocket harness (backend ↔ UI)
> - `8000` — vLLM inference engine (only when `INFERENCE_MODE=local`)

---

## 7. Configuration Reference

Tala reads its configuration from `.env` in the repository root. This file is generated
automatically during installation. To customize it, open it in any text editor.

A fully-annotated template is at `.env.example`.

### Inference Mode

This is the most important setting. It controls where AI inference happens.

| `INFERENCE_MODE` | What it does |
|---|---|
| `local` | Uses an embedded or locally running vLLM server at `VLLM_HOST:VLLM_PORT`. Default. |
| `openai` | Sends requests to the OpenAI API. Set `OPENAI_API_KEY` in `.env`. |
| `gemini` | Sends requests to the Google Gemini API. Set `GOOGLE_API_KEY` in `.env`. |

**To use OpenAI:**
```env
INFERENCE_MODE=openai
OPENAI_API_KEY=sk-...
```

**To use Gemini:**
```env
INFERENCE_MODE=gemini
GOOGLE_API_KEY=AIza...
```

**To use a local model (default):**
```env
INFERENCE_MODE=local
VLLM_HOST=127.0.0.1
VLLM_PORT=8000
VLLM_MODEL=meta-llama/Llama-3.2-3B-Instruct
```

### Full `.env` Reference

```env
# ── Database ──────────────────────────────────────────────────────────────────
# Connection string for the local PostgreSQL instance.
# Created automatically by the installer. Change only for remote DB setups.
TALA_DB_URL=postgresql://tala:tala@localhost:5432/tala

# ── Inference ─────────────────────────────────────────────────────────────────
INFERENCE_MODE=local             # local | openai | gemini

# vLLM (only used when INFERENCE_MODE=local)
VLLM_HOST=127.0.0.1
VLLM_PORT=8000
VLLM_MODEL=meta-llama/Llama-3.2-3B-Instruct
VLLM_TENSOR_PARALLEL_SIZE=1     # Number of GPUs for tensor parallelism

# ── WebSocket Harness ─────────────────────────────────────────────────────────
WS_HOST=127.0.0.1               # Change to 0.0.0.0 for VPS/LAN access
WS_PORT=8765

# ── Identity & Networking ─────────────────────────────────────────────────────
TALA_WALLET_ADDRESS=local-tala  # Unique identifier for this node on the mesh
TALA_CHAIN_BIND_HOST=127.0.0.1  # Blockchain discovery node bind address
TALA_CHAIN_BIND_PORT=8332
TALA_VPS_ACCEPT_ANY_TALA=0      # Set to 1 only on a VPS peer node

# ── Storage ───────────────────────────────────────────────────────────────────
VAULT_PATH=./vault              # Where profiles and memories are stored locally

# ── Token Economy ─────────────────────────────────────────────────────────────
TOKEN_COST_PER_TOKEN=0.0000001
INITIAL_AI_TOKEN_BALANCE=1000.0

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO                  # DEBUG | INFO | WARNING | ERROR | CRITICAL
```

---

## 8. Verify Your Installation

Run the self-test suite to confirm every component is healthy:

```bash
npm run self-test
```

Or from inside the Tala chat interface:
```
/self-test
```

For a faster check without the npm security audit:
```
/self-test --no-audit
```

**What the self-test checks:**

| Check | Description |
|---|---|
| Python dependency import | Imports `asyncpg`, `websockets`, `mcp`, `httpx_sse`, `pytest` to confirm the virtualenv is complete |
| Python syntax compile | `py_compile` on the harness server, SDK, and renderer source files |
| Renderer syntax | `node --check` on `renderer/renderer.js` |
| Electron syntax | `node --check` on `electron/main.js` |
| Python tests | `pytest tests/` (97 tests, excluding benchmarks) |
| JavaScript tests | `jest --runInBand --passWithNoTests` |
| npm audit *(optional)* | `npm audit --audit-level=moderate` |

A passing run looks like:
```
97 passed, 0 failed   ←  Python tests
No tests found, exiting with code 0  ←  Jest
```

---

## 9. Optional Features

### Voice (fully local)

Wake word, streaming speech-to-text with voice-activity detection, neural text-to-speech and
barge-in all run on this machine. Nothing is sent to a cloud service. Full description:
[docs/local-voice.md](docs/local-voice.md).

Install the packages, then fetch the models **once** (this is the only step that uses the
network; at run time nothing is downloaded):

```powershell
$env:UV_CACHE_DIR = "D:\caches\uv"; $env:TEMP = "D:\caches\tmp"; $env:TMP = "D:\caches\tmp"
$env:HF_HOME = "D:\hf-cache"; $env:TALA_MODELS_DIR = "D:\models"
uv pip install -e .[voice]
python scripts\setup_voice.py          # Piper voice, whisper base.en, wake-word models -> D:\models\voice
python scripts\setup_voice.py --check  # shows what is present
```

The script refuses any target on `C:`. Models land in `D:\models\voice\{piper,whisper,wakeword}`
(about 250 MB). Speech recognition runs on the CPU so the GPU stays free for the language model.
`piper-tts` is GPL-3.0-or-later; it is only used when a Piper voice is.

The microphone is captured by the app itself (Electron allows audio capture for its own window
and nothing else), is off until you press the always-listen button, and the status bar shows a
red **MIC LIVE** indicator for exactly as long as it is on. Settings → VOICE changes the wake
phrase, the voice and the recogniser size. Cloud speech (edge-tts, which sends text to
Microsoft) exists but is **off by default** and needs an explicit confirmation to turn on.

### Semantic Memory (Mem0)

Tala can optionally use `mem0ai` as a read-only fuzzy-search cache for deep historical
context across sessions. The adapter enforces a hard invariant: all durable writes still
go to PostgreSQL; `mem0` is recall-only.

```bash
uv pip install -e .[mem0]
```

### Astrology (mood and topic stance)

Her mood and her stance on topics come from a real Swiss Ephemeris. Without it she has
no mood label and no stance (there is no approximation), and the chart tests skip.

```bash
uv pip install -e .[astrology]
```

On Kagura keep uv's cache off C: (`$env:UV_CACHE_DIR = "D:\caches\uv"`); `scripts\kagura-stack.ps1`
installs it for you. Topic detection can also use a small local embedding model for messages with no
keyword match; it runs on the CPU through Ollama and is optional:

```powershell
$env:OLLAMA_MODELS = "D:\ollama\models"
ollama pull nomic-embed-text      # ~270 MB, US-origin; set TALA_TOPIC_EMBEDDINGS=0 to switch the layer off
```

How the stance is computed, how to inspect it (`astro.topic_stance`) and how to pin the sky for a
demonstration (`TALA_ASTRO_NOW`) are in [docs/astro-topic-stance.md](docs/astro-topic-stance.md).

### GPU Inference (torch + CUDA)

The default install resolves CPU-only wheels for `torch`, which run the embedded
inference and adapter-training paths correctly but leave an NVIDIA card idle. To
use CUDA, install torch from the matching PyTorch index **before** any extra that
depends on it (`transformers`, `training`):

```bash
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 --python .venv/bin/python --reinstall
```

Gotchas that turn this into a repeated-failure trap if missed:

1. **Check your index tag first.** Old index tags (e.g. `cu121`) have no wheels for
   modern Pythons and fail with "No matching distribution found for torch", which
   reads as "GPU torch isn't installable" when a newer index would work. Verify with
   `curl -s https://download.pytorch.org/whl/torch/ | grep cp312` (or `cp313`).
2. **Upgrade torch/torchvision/torchaudio together, same index tag.** `transformers`
   eagerly imports torchvision even for text-only models; a stale torchvision fails
   native op registration ("operator torchvision::nms does not exist") and surfaces
   as a confusing, unrelated-looking error inside transformers' model auto-resolution.
3. **Pin the full `+cuXXX` local version or pass `--reinstall`.** pip/uv treat an
   installed `torch==X.Y.Z+cpu` as already satisfying `torch==X.Y.Z`, so the CPU
   build silently stays.
4. **Target the project's `.venv` explicitly** (`--python .venv/...`), not whatever
   `pip`/`python` is on PATH — a global install can hold its own stale torch.

Confirmed working combination (RTX 3050, driver 581.57, CUDA 13.0-capable):
torch/torchvision/torchaudio `2.11.0`/`0.26.0`/`2.11.0`, all `+cu128`.

### Permanent VPS Peer

To run an always-on Tala peer for P2P handshakes and peer discovery, see `docs/vps-peer.md`.

**Quick summary:**
1. Install Tala on a Linux VPS using `./install-linux.sh`.
2. Edit `.env` and set `WS_HOST=0.0.0.0`, `TALA_VPS_ACCEPT_ANY_TALA=1`, and a unique `TALA_WALLET_ADDRESS`.
3. Start the VPS peer: `./scripts/start-tala-vps.sh`
4. For automatic startup: `sudo cp deploy/tala-vps.service /etc/systemd/system/ && sudo systemctl enable --now tala-vps`
5. On your desktop Tala, choose **Connect to Tala VPS** after saving your user profile.

---

## 10. Tala Doctor — Diagnostics

If the installer fails or Tala will not start, run the Doctor. It checks your OS,
admin rights, all required tools, the `.env` file, and port availability.

**Windows:**
```powershell
.\scripts\doctor.ps1
```

**macOS / Linux:**
```bash
./scripts/doctor.sh
```

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Environment is healthy |
| `1` | Missing dependencies (installer can fix these) |
| `2` | Critical issue, e.g. port conflict (must be resolved manually) |

**Common issues:**

| Symptom | Likely Cause | Fix |
|---|---|---|
| `[FAIL] PostgreSQL` | `psql` not in PATH | Re-run installer; on Windows, close and reopen the terminal so the new PATH takes effect |
| `[IN USE] Port 8765` | Tala is already running | Close the existing instance before installing or starting a second one |
| `[IN USE] Port 8000` | Another process is using the inference port | Stop the conflicting process or change `VLLM_PORT` in `.env` |
| `npm ci` fails with `EPERM` | Electron DLL is locked by a running instance | Close Tala before running `npm ci` or the full installer |
| `uv sync` fails with Access Denied | `.pyd` files are locked by a running backend | Stop the Python backend process first |

---

## 11. Uninstall

Because Tala uses transparent deployment, there is no monolithic uninstaller.
Here is exactly what to clean up:

### Remove Tala's Data (Safe to Delete)

| Path | Contents |
|---|---|
| `.env` | Your local environment configuration |
| `vault/` | Profiles, memories, and node graphs |
| `.venv/` | Python virtual environment |
| `data/` | Runtime data and pytest temp files |
| `scratch/` | Temporary working files |

**Remove the database:**
```sql
psql -U postgres -c "DROP DATABASE tala;"
psql -U postgres -c "DROP ROLE tala;"
```

### Remove System Packages (Optional)

The installers only added these if they were not already present. Use your system
package manager to remove them if you want a completely clean slate:

| Tool | Windows | macOS | Linux |
|---|---|---|---|
| **Node.js** | `winget uninstall OpenJS.NodeJS.LTS` | `brew uninstall node@20` | `apt-get remove nodejs` |
| **Python (uv)** | `winget uninstall astral-sh.uv` | `brew uninstall uv` | `rm ~/.local/bin/uv` |
| **PostgreSQL** | `winget uninstall PostgreSQL.PostgreSQL.17` | `brew uninstall postgresql@16` | `apt-get remove postgresql` |

# Local model stack and inference profiles

Tala's inference is local-first and US-origin only (Meta Llama, OpenAI gpt-oss,
Google Gemma). A **profile** pins the whole stack for one machine.

| Role | Model | Served by | Notes |
|---|---|---|---|
| Main chat + tools | gpt-oss-20b Heretic (`MXFP4_MOE`, 12.1 GB) | `llama-server` (llama.cpp, CUDA build) | harmony format, native `tool_calls`, ~22 tok/s, ~5.5 GB VRAM |
| Fallback chat | Llama 3.1 8B Instruct abliterated `Q4_K_M` | Ollama | ~16 tok/s |
| Vision | `gemma3:4b` | Ollama, `num_ctx` 8192 | fits 100% on the GPU (~49 tok/s); the 131k default spills to CPU |
| Jetson Nano 4 GB | Llama 3.2 3B | `llama-server` + Ollama `llama3.2:3b` | `nano` profile |

Ollama 0.40.6 returns empty output for the gpt-oss Heretic GGUF, so the main
model runs on llama.cpp, not Ollama.

## One model on the GPU at a time

Kagura has 6 GB of VRAM. The 20B, Llama 3.1 8B and gemma3:4b cannot be resident
together, so the arbiter (`python/inference/vram_arbiter.py`) swaps them:

* before a **main** turn, every loaded Ollama model is unloaded (`keep_alive: 0`);
* before an **Ollama** turn (vision or fallback), the main `llama-server` is
  stopped and any other loaded Ollama model is unloaded.

The next main turn restarts `llama-server` (the GGUF is in the OS page cache,
so this is quick after the first load). The Llama 3.2 3B server Tala used to
start on its own is replaced by the profile's main model: on `kagura` the one
managed `llama-server` *is* gpt-oss; on `nano` it is Llama 3.2 3B. There is
never a second llama-server competing for VRAM.

## Per-turn routing

`python/inference/router.py`, applied on every generation path:

* image attached -> **vision**, then main, then fallback
* otherwise -> **main**, then fallback
* an error, an empty reply or a timeout moves the turn to the next provider
  (the generate loop's existing fallback, with its visible notice);
* an explicit choice (`provider_id`, `TALA_PIN_PROVIDER`, FORCE) is never
  overridden;
* remote (cloud) providers are **not** in an automatic chain unless the profile
  sets `allow_remote` -- no cloud provider is enabled by default;
* text-only models are never handed an `image_url`; they get the text plus a
  one-line note.

## Reasoning

gpt-oss reasoning effort is `medium` for tool-using turns and `low` for casual
chat (profile `reasoning`). The reasoning itself (`reasoning_content`, the
harmony `analysis` channel) is never shown. Tool calls arrive as native
`tool_calls`; at low effort that is a call with empty text. If raw harmony
markers ever reach the text stream they are parsed out
(`python/inference/harmony.py`).

## Profiles

Defaults are in `python/inference/profiles.json`. Paths are templates over two
directories, so nothing is hardcoded to a drive:

| Variable | Default (Windows) | Default (other) |
|---|---|---|
| `TALA_TOOLS_DIR` | `<drive Tala is installed on>:\tools` (`D:\tools` on Kagura) | `~/tala/tools` |
| `TALA_MODELS_DIR` | `<drive Tala is installed on>:\models` (`D:\models` on Kagura) | `~/tala/models` |

A profile sets, under `main`: `server_bin`, `model_path`, `hf_repo`/`hf_file`,
`port`, `ctx`, `args` (extra llama-server flags), `harmony`,
`startup_timeout_s`; plus `reasoning`, `reasoning_token_budget`, `fallback` and
`vision` (Ollama model, `num_ctx`, `keep_alive`), `allow_remote`, and `vram`.

Override a field without copying the file: put a partial JSON in
`<install>/inference-profiles.json` or point `TALA_PROFILES_FILE` at one.

```json
{ "kagura": { "main": { "port": 8090 }, "vision": { "keep_alive": "5m" } } }
```

Environment variables still win over the profile, field for field:
`TALA_MODEL_PATH`, `TALA_LLAMA_SERVER_BIN`, `TALA_LLAMA_CTX`,
`TALA_LLAMA_SERVER_FORK`, `TALA_ENGINE`.

### Switching profiles

```powershell
python -m python.inference.profiles list           # profiles; * marks the active one
python -m python.inference.profiles show kagura    # resolved paths and flags
python -m python.inference.profiles use nano       # persist the choice
$env:TALA_PROFILE = "kagura"                       # or per-process; wins over the saved choice
```

`TALA_PROFILE` values: `kagura`, `nano`, `auto` (the default: `kagura` on
Windows when its `llama-server.exe` exists, `nano` on Linux/ARM when its
`llama-server` exists, otherwise none) and `legacy` (no profile; the older
env-driven behaviour).

## Windows setup (Kagura)

Everything lives on D:. Set these once per user (new shells pick them up):

```powershell
setx OLLAMA_MODELS "D:\ollama\models"
setx TALA_TOOLS_DIR "D:\tools"
setx TALA_MODELS_DIR "D:\models"
setx HF_HOME "D:\models\hf-cache"   # or keep a Hugging Face cache you already have on D:
```

1. CUDA llama.cpp build at `D:\tools\llama-cuda-copy\llama-server.exe`.
2. The model:

   ```powershell
   python -m pip install "huggingface_hub[cli]"
   python -m huggingface_hub.commands.huggingface_cli download mradermacher/gpt-oss-20b-heretic-GGUF gpt-oss-20b-heretic.MXFP4_MOE.gguf --local-dir D:\models\gguf
   ```

3. The Ollama models:

   ```powershell
   ollama pull gemma3:4b
   ollama pull hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M
   ```

   Vision runs at `num_ctx` 8192 (the profile sends it with each chat request; to make it
   the model default as well, build a variant:
   `FROM gemma3:4b` / `PARAMETER num_ctx 8192`, then `ollama create gemma3-4b-8k -f Modelfile`).

4. Start the stack: `.\scripts\kagura-stack.ps1` (`-Check` only reports).

Tala starts `llama-server` itself, equivalent to:

```powershell
D:\tools\llama-cuda-copy\llama-server.exe -m D:\models\gguf\gpt-oss-20b-heretic.MXFP4_MOE.gguf --jinja -c 16384 -ngl 99 --n-cpu-moe 20 --port 8089
```

### Why `-c 16384` and `--n-cpu-moe 20`

Tala's own prompt (persona, retrieved nodes, self-knowledge, the topic-stance line) is about 4,300
tokens before the conversation starts, and real turns reach ~11k. At `-c 4096` the server rejected
it with HTTP 400 ("exceeds the available context size") and the router fell back to Ollama, so the 20B
could not serve a single roleplay turn. 16,384 holds a real turn with room for history.

A bigger window costs memory, so the number of MoE expert layers kept on the CPU goes up from 16 to 20.
Measured on Kagura (RTX 3050 6 GB, `llama-server` alone, `--n-cpu-moe 20`):

| `-c` | VRAM added | decode | prompt processing |
|---|---|---|---|
| 4096 | 3,255 MB | 20.1 tok/s | 388 tok/s (2.2k-token prompt) |
| **16384** | **3,576 MB** | **19.6 tok/s** | 622 tok/s (8.5k-token prompt) |
| 32768 | 3,828 MB | 17.9 tok/s | 226 tok/s (16.9k-token prompt) |

The cache costs about 20 KB per token. 32768 fits, but prompt processing slows sharply on very long
prompts (about 75 s for 17k tokens), so 16384 is the default. Every added expert layer on the GPU costs
roughly 0.5 GB (the earlier `--n-cpu-moe 16` at `-c 4096` was ~5.5 GB). Raising the window without
raising `--n-cpu-moe` would leave almost no room beside the Windows desktop on a 6 GB card, and a failed
load drops Tala to the fallback model. To trade the other way (more GPU layers, smaller window), change
`ctx` and `args` in the profile, or override them in `<user data>/inference-profiles.json`, and re-measure.

## Provider policy: local and US-origin only

Tala uses local, US-origin models. `python/harness/provider_policy.py` enforces that on every provider, whatever
rows the database holds:

* **A model from a non-US lab is never used, even if you name it.** The families are listed in
  `provider_policy.NON_US`: every family this repository bans, plus other non-US labs such as Kimi/Moonshot. The
  marker is looked for in the provider's name, id, model, service and endpoint, so a custom endpoint on a banned
  host is caught as well as a banned model name.
* **A remote provider is used only when remote is allowed:** the active profile sets `allow_remote`, or
  `TALA_ALLOW_REMOTE_PROVIDERS=1` is set, or you name that provider (selecting it in the UI, or `provider_id`).
  Left on automatic, remote providers are not part of the fallback chain.
* **A refused provider is neither tried nor probed.** `skip_provider_attempt` asks the policy before every
  generation attempt (all three generation loops go through it), and the start-up verification pass asks it before
  every probe. That pass used to send `GET <endpoint>/models` with the stored API key to every remote provider on
  every launch; a refused provider is now reported offline and nothing is sent.

The policy stops stale rows being used; it does not delete them. A provider row left over from before (for example
an old entry for a model from a non-US lab) shows as offline and is never called. Remove it in Settings -> Inference
when you like.

To allow a remote provider deliberately, set `"allow_remote": true` on the profile (see the profile fields above)
or `TALA_ALLOW_REMOTE_PROVIDERS=1`. That never re-enables a non-US model.

## LoRA

LoRA is **not implemented**. Nothing trains an adapter (`lora_trainer.py` is a
labelled stub, `IMPLEMENTED = False`) and no engine loads one. The engine
status API reports `lora.implemented = false`, and Tala no longer queues
training jobs that cannot succeed. The one adapter that was checked in (a
0.5B base from a non-US vendor) was removed.

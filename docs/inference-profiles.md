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

* before a **main** turn, every loaded Ollama model that holds VRAM is unloaded
  (`keep_alive: 0`); a model running on the CPU is left alone;
* before an **Ollama** turn on the GPU (the fallback), the main `llama-server` is
  stopped and any other loaded Ollama model is unloaded;
* before an Ollama turn on the **CPU** (`num_gpu: 0`, which is how vision runs on
  `kagura`), nothing happens at all: it takes no VRAM.

Restarting `llama-server` is not quick: 53 s to load, whatever the settings. Vision therefore runs on
the CPU (see "Image turns" below), so the main model is never stopped for it. The Llama 3.2 3B server Tala used to
start on its own is replaced by the profile's main model: on `kagura` the one
managed `llama-server` *is* gpt-oss; on `nano` it is Llama 3.2 3B. There is
never a second llama-server competing for VRAM.

## Per-turn routing

`python/inference/router.py`, applied on every generation path:

* image attached -> the **vision** model describes it (see "Image turns"), then **main** answers, then fallback
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
D:\tools\llama-cuda-copy\llama-server.exe -m D:\models\gguf\gpt-oss-20b-heretic.MXFP4_MOE.gguf --jinja -c 16384 -ngl 99 --n-cpu-moe 22 -ub 4096 -b 4096 --port 8089
```

### Image turns

An image turn used to hand gemma3:4b Tala's entire assembled prompt (persona, retrieved nodes, tool schemas,
history): 8,807 tokens measured, against gemma's 8,192-token window. It answered HTTP 400, the request fell back to the
main model, and that meant stopping gemma, restarting `llama-server` (53 s) and answering without ever seeing the picture
("Could you tell me the number?" to a picture of a number). 110 s, and the wrong answer.

Now `python/inference/vision.py` asks the vision model one small question, the image and what the user asked (a prompt
of a few hundred tokens), and puts its description into the main model's prompt. The main model answers as itself, with
its persona, its tools and its 16k window. The alternative, letting gemma answer directly, is the route that failed above; it
was not tried on the CPU, where it would mean reading that 8.8k-token prompt with a 4B model (an inference, not a measurement),
and it would still leave the persona and tools out of the answer.

The vision pass runs on the **CPU** (`vision.gpu: false` in the profile sends Ollama `num_gpu: 0`), so it takes no VRAM and
the main model is never stopped. Kagura has 128 GB of RAM, so gemma stays resident for 30 minutes (`keep_alive`). With the
vision model on the GPU instead, the pass itself took 2.3 s but the main server has to stop and restart, 53 s in every one of
the 18 benchmark loads, for an image turn of a minute or more against 11 s on the CPU. The first image after Ollama has been idle also
pays for loading the model into RAM (about 20 s here); later ones take about a second. If the vision model or Ollama is
unavailable the turn simply carries on without a description, exactly as before: the image stays attached and a main model that
cannot see it says so. `profiles.json` -> `vision.gpu` switches it back to the GPU on a machine with VRAM to spare.

### Where a turn's time went

The same stack, the same questions, before and after (Kagura, 2026-10-23, desktop idle; "before" is `--n-cpu-moe 20` at the
default micro-batch with vision on the GPU, and the per-turn settings refresh described below):

| | before | after |
|---|---|---|
| plain text turn, wall clock | 27.2 to 27.7 s | 9.3 to 10.7 s |
| time to first token | 8.7 to 9.7 s | 3.8 to 4.3 s |
| decode | 19.7 to 20.3 tok/s | 18.7 to 19.2 tok/s |
| ~10k-token conversation (a planted word recalled, no truncation) | 156 s, first token 26.8 s | 19.0 s, first token 11.9 s |
| image turn | 109.8 s, answered wrongly, llama-server restarted | 11.4 s, answered correctly, no restart |
| text turn after the image | 27.7 s | 9.6 s |
| llama-server restarts across an image turn and the next text turn | 1 (53 s) | 0 (same process id throughout) |

Decode is about 5% slower (`--n-cpu-moe 22` against 20) in exchange for halving prefill time; for the short answers a chat
turn gives, that is the right trade.

Most of a short turn was not inference. The harness regenerated **all thirteen settings nodes on every turn**
(`server.py`, "Dynamically load all 12 Harness Settings Panel Neural Nodes": a database read, a ledger anchor and a file write
each, about 13 s) only to read four of them for the live settings snapshot. They are now refreshed in the background, at most
every five minutes (`settings_nn.refresh_in_background`); the nodes are still written at startup and by the settings RPCs when
something changes, and `get_system_settings_info` still forces a full refresh when the model asks. A refresh that overlapped a
turn still slowed it by about 10 s while it ran, which is why the interval is minutes, not seconds.

What is left outside inference, per turn: about 2.3 s in the token gate (`token_counter`, logged as `Token gate PASSED ...
elapsed=`, ledger writes) and about 1 s asking for the emotional state. Not measured on an install with the chain node running.

### Status bar

After each reply the status bar shows the speed it came back at: `19.0 tok/s - first token 4.0 s`. The harness attaches a `timing`
object to the reply (`python/inference/latency.py`), taken from what the provider reported: llama-server's own `timings` and the
measured time to the first streamed token, or Ollama's `eval_count` / `eval_duration`. A reply no provider reported on clears the
readout rather than leaving the last one standing; hover for the prompt size and prompt speed.

### Keeping the main model up

`llama_server_manager._supervise` restarts `llama-server` with backoff if it exits. A server that is alive but has stopped
answering was not noticed, so a watchdog now asks `/health` every 15 s and, after three failures in a row, stops it so that
the supervisor brings it back (a server that will not stop is killed). One slow answer is not a failure.

### Why `-c 16384`, `--n-cpu-moe 22` and `-ub 4096`

Tala's own prompt (persona, retrieved nodes, self-knowledge, the topic-stance line) is about 4,300
tokens before the conversation starts, and real turns reach ~11k. At `-c 4096` the server rejected
it with HTTP 400 ("exceeds the available context size") and the router fell back to Ollama, so the 20B
could not serve a single roleplay turn. 16,384 holds a real turn with room for history.

These settings were chosen by measurement, not by rule of thumb: `scripts/bench_kagura.py` starts
`llama-server` on its own port for each configuration and records VRAM, time to first token, prefill
and decode speed on a short and an 8k-token prompt (2 trials each, median). Run it with the GPU otherwise
idle; it refuses to run with less than 4.5 GB free, because a number taken beside a game describes the game.

```powershell
$env:TEMP = "D:\caches\tmp"; $env:TMP = $env:TEMP; $env:HF_HOME = "D:\hf-cache"
.venv\Scripts\python scripts\bench_kagura.py --stage all --model D:\models\gguf\gpt-oss-20b-heretic.MXFP4_MOE.gguf
```

Results are written to `D:\tala-audit\bench`. On Kagura (RTX 3050 6 GB, desktop idle, 2026-10-23), at `-c 16384` with an 8,000-token prompt:

| setting | VRAM | prefill | time to first token | decode |
|---|---|---|---|---|
| before: `--n-cpu-moe 20` | 3.6 GB | 426 tok/s | 19.2 s | 20.0 tok/s |
| `--n-cpu-moe 16` | 4.4 GB | 160 tok/s | 50.9 s | 16.5 tok/s |
| `--n-cpu-moe 18` | 4.4 GB | 461 tok/s | 17.8 s | 21.5 tok/s |
| `--n-cpu-moe 22` | 2.8 GB | 416 tok/s | 19.7 s | 19.1 tok/s |
| `--n-cpu-moe 24` | 2.0 GB | 398 tok/s | 20.6 s | 17.8 tok/s |
| `--n-cpu-moe 20 -ub 1024` | 3.7 GB | 676 tok/s | 12.2 s | 20.0 tok/s |
| `--n-cpu-moe 20 -ub 2048` | 3.9 GB | 926 tok/s | 9.0 s | 19.9 tok/s |
| `--n-cpu-moe 20 -ub 4096` | 4.5 GB | 1,115 tok/s | 7.5 s | 19.8 tok/s |
| **`--n-cpu-moe 22 -ub 4096`** | **3.7 GB** | **1,092 tok/s** | **7.6 s** | **18.5 tok/s** |
| `--n-cpu-moe 24 -ub 4096` | 2.9 GB | 1,065 tok/s | 7.8 s | 17.5 tok/s |

What the numbers say:

* **The micro-batch is the big lever.** Prefill is the cost of a long prompt, and the default micro-batch of 512
  left it at about 430 tok/s, so 8k tokens took 19 s before the first word. With the experts on the CPU, a bigger
  micro-batch amortises moving their weights to the GPU: 4096 more than doubles prefill and leaves decode alone.
* **Fewer experts on the CPU is a trap.** `--n-cpu-moe 16` nearly fills the card and collapses (51 s to first
  token); `18` is a few percent faster than `20` but sits 1 GB from that cliff, and the desktop's own VRAM use varied by
  gigabytes during these measurements. The choice leaves at least 2 GB of headroom (`--vram-cap-mb`, 4100).
* **Context is free for decode.** Decode stayed at 21 to 22 tok/s from 4k to 16k. A longer window only costs memory and
  the time to read a long prompt, which is why prefill speed matters more than any other setting here.
* **KV cache `q8_0` and forced flash attention buy nothing.** This build already runs flash attention on `auto`; `q8_0`
  saves about 200 MB at a 5% decode cost, and the 16k cache is small for this model.
* Earlier measurements (`--n-cpu-moe 20`, default micro-batch): `-c 4096` 20.1 tok/s, `-c 16384` 19.6 tok/s,
  `-c 32768` 17.9 tok/s with prefill falling to 226 tok/s (75 s for a 17k-token prompt), which is why the window stays at 16384.

To trade the other way (more GPU layers, smaller window, a different card), change `ctx` and `args` in the profile, or
override them in `<user data>/inference-profiles.json`, and re-measure with the script.

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

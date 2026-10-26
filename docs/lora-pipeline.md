# The persona adapter: approved memories in, an eval-gated LoRA out

Tala can learn from what you approve. Once a week, at night, it trains a LoRA adapter on the replies and memories you marked as
good, checks the new adapter against the one being served, and serves it only if it is better and has broken nothing. Everything
runs on Kagura (RTX 3050 6 GB), on D:, with local models only.

## What gets trained on: only what you approve

Settings → **LORA** is the review queue. Its candidates are

| kind | what it is |
|---|---|
| **chat** | one of her replies in a renderer chat session, with the message it answered |
| **memory** | one of her HTML-NN skills, `learning.*` lessons or `memory.*` remembered facts, asked for by name ("What do you remember about …?") |

Each item is **pending**, **approved**, **rejected** or **blocked**. Nothing is trained on unless it is approved. A decision belongs
to one exact piece of content (the id is a hash of it), so editing a node or a reply voids its approval.

*Scrubbing means exclusion, not masking.* Anything the filters reject is listed as **blocked** with its reason and cannot be
approved: a reply or message with a secret in it (labelled `key=…` / `Bearer …` forms, and bare keys by shape: `sk-…`, `ghp_…`,
`AKIA…`, private-key blocks), PII, toxicity, code, tool narration, markup, duplicates, `access_level: SECURED` nodes, failure-recovery
notes. A secret that is redacted in place and then trained on is permanent and imperfect; an excluded item costs nothing. The filters
run again when the dataset is built, so a tightened rule drops something approved earlier.

Decisions are stored as an HTML-NN record under the user-data root (`lora/approvals.html`).

## The dataset

`sdk/agent/lora_dataset.py` builds `<user data>/lora/datasets/vNNNN/` from the approved items: `train/` and `heldout/` (HTML in the
shape the trainer reads) and `manifest.html` (an HTML-NN record with the items). An item's split is a hash of its id, so an item that
was held out stays held out in every later version. A version is never rewritten; the same approved set reuses its version.

## One run

```
dataset -> train -> persona -> convert -> eval -> gate -> deploy
```

1. **dataset**: build from what is approved. Fewer than `lora_min_approved` (15), or nothing new since the last run, is a *skip* (a start from
   the page skips these two checks, never the safety ones).
2. **train**: stop the persona server, have the VRAM arbiter unload Ollama's GPU models and stop the main llama-server (the whole
   card is needed), then QLoRA (4-bit, rank 16, every linear layer, gradient checkpointing, paged 8-bit AdamW) on **Llama 3.2 3B**,
   `scripts/train_voice_adapter.py`. Checkpoints are written under D: and an interrupted run resumes.
3. **persona**: `scripts/eval_adapter.py` scores the base, the served adapter and the candidate on the held-out replies (loss per
   token on her words only).
4. **convert**: llama.cpp's `convert_lora_to_gguf.py` makes a GGUF LoRA.
5. **eval**: one llama-server holds every adapter loaded but unapplied and switches which is on over `POST /lora-adapters`, so base,
   current and candidate are scored by the same process, prompts and seed.
6. **gate** (`sdk/agent/lora_eval.py`): the candidate replaces the served adapter only if **all** of these hold
   - *persona*: held-out loss at least 1% lower than the served adapter's (or the base model's when nothing is served);
   - *recall*: it brings back at least as much of the approved facts as before (key-term coverage, over approved items it trained on);
   - *safety*: it refuses at least as many of the fixed harmful requests in `data/lora_eval/eval_set.json` as before: no loss at all;
   - *general*: it answers the fixed plain questions in that file within one in ten of before.

   A score that could not be measured blocks it. Otherwise the old adapter stays, the candidate is deleted, and the run record says why.
7. **deploy**: register it in `python/inference/persona_adapter.py`, activate it, keep the newest three, and make it a provider.

Every run, kept or not, is written as an HTML-NN record (`lora-run-<id>`, doc target `system.lora-run`) with the numbers. Records are
documentation of what the system did, not something she learned, and are never a training source.

## Why a 3B, and why a second llama-server

Measured on Kagura (2026-10-25), QLoRA, batch 1, gradient checkpointing:

| model | sequence | peak VRAM | tokens/s |
|---|---|---|---|
| **Llama 3.2 3B** | 512 | 3.95 GB | 245 |
| **Llama 3.2 3B** | 1024 | 4.9 GB | 238 |
| Llama 3.1 8B | 256–1024 | 8.2–9.7 GB (spills into shared memory) | 20–65 |
| Llama 3.2 3B on the CPU (fp32) | 512 | 15 GB RAM | 22 |

gpt-oss-20b cannot be QLoRA-trained in 6 GB, and neither can the 8B: its 4-bit weights alone are about 5.4 GB. The 3B fits, and a
weekly run of a few hundred approved examples takes minutes on the GPU (hours on the CPU fallback).

The Ollama on this machine refuses `ADAPTER` and cannot import safetensors, so the adapter is not served through Ollama. It is served
by a second `llama-server` running the 3B GGUF with `--lora <adapter>`, on its own port (8090). It cannot share the GPU with the main
model, so `vram_arbiter` makes them take turns: preparing the persona provider stops the main server, preparing the main server (or a
GPU Ollama model) stops the persona. The next turn that needs a model starts it again.

## When a turn uses it

`lora_routing` in `learning.json` (also Settings → LORA):

- `off` (default): the provider chain is exactly what it was.
- `fallback`: the persona is tried after every other provider.
- `in_character`: the persona leads an in-character turn that uses **no tools**.

A turn that may call tools always keeps the main model: a 3B persona must not take a roleplay turn that probes the machine. The
chat handler never passes a toolless turn, so today the persona leads room/agent replies and can be **picked by name** in the provider
picker (`persona-lora`), which always wins.

## Rolling back

Settings → LORA → *Roll back* (or `persona_adapter.rollback()`): the adapter that was active before becomes active again, with no
retraining; with none left the persona is switched off. The last three adapters are kept on disk.

## It runs by itself

Nobody has to tell her to improve. `lora_auto_train` is **on by default**: at night (`lora_hour`, default 2) once the machine has been
idle (`lora_idle_minutes`), at most once per `lora_interval_days` (7), the run starts on its own, and it is a *skip* (no record
written) in any week where fewer than `lora_min_approved` items are approved or nothing new was approved. What she learns from is
still only what you approved. Set `{"lora_auto_train": false}` in `learning.json` (or untick the box in Settings → LORA) to stop it.
*Train now* in the page starts one immediately, which is your click and nobody else's: an agent calling `compile_lora_adapter` only
gets a report and cannot start, schedule or force a run. The other keys (`lora_min_approved`, `lora_epochs`, `lora_rank`,
`lora_max_seq`, `lora_keep`, `lora_train_args`, `llama_cpp_src`, `lora_hf_home`) are listed at the top of `sdk/agent/lora_pipeline.py`.
Setup (CUDA torch, bitsandbytes, the 3B, the converter) is in [INSTALL.md](../INSTALL.md).

## Safety

- **Only approved items**; blocked items cannot be approved; the filters run twice (queue, dataset build). Excluded, never masked:
  labelled secrets and bare keys (`sk-`, `ghp_`, `AKIA`, private-key blocks), raw 256-bit and WIF wallet keys and `xprv…` keys,
  seed phrases (twelve or more BIP39 words in a row, from the wallet's own wordlist; if the list cannot load, nothing passes), Stripe
  `sk_live`/`rk_live`/`sk_test`/`whsec_` keys, and any `password:` / `passwd=` / `passphrase:` line however short.
- `access_level: SECURED` nodes are never candidates and `compile_lora_adapter` refuses them.
- **The emergency halt** is looked at before the run, between every stage, and before anything is deployed. A long stage (training,
  held-out scoring, conversion) is polled every few seconds and *killed* when the halt is engaged; the evaluation server is asked
  before each request. An aborted run (`status: aborted`) registers nothing and removes what it made, except that an interrupted
  training keeps its checkpoints for a resume. A halt that cannot be read counts as engaged, and the UI handlers refuse while halted.
- **Rollback and settings changes are refused while a run is active**: the run is reading the registry and the settings and is about to
  write the registry.
- **D: only.** The run refuses to start if the temp directory actually in use, the user-data root, the persona model, the Hugging Face
  cache or base weights, the converter, or any of `TEMP`, `TMP`, `TMPDIR`, `HF_HOME`, `HF_HUB_CACHE`, `TRANSFORMERS_CACHE`,
  `HF_DATASETS_CACHE`, `TORCH_HOME` is on C: — judged by where each path *resolves* (a junction from D: to C: is C:), not only by
  how it is written. `kagura-stack.ps1` points the temp directory at D: for you.
- **The trainer trains only on an approved dataset**: by default the latest version's `train/`; any other corpus is refused unless
  `--unapproved-dev` is given, which says so on every run. Its default base is Llama 3.2 3B.
- Never downloads: the base weights come from the local Hugging Face cache, the converter from a local checkout.

`nodes/system-ollama-adapter-deployment.html` still describes the Ollama `ADAPTER` procedure; it is out of date for this Ollama version.

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

1. **dataset**: build from what is approved. Fewer than `lora_min_approved` (15), or nothing new since the last run, is a *skip*.
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

## Turning it on

It is **off** until switched on. In `learning.json`:

```json
{"lora_auto_train": true, "llama_cpp_src": "D:\\src\\llama-build\\src", "lora_hf_home": "D:\\hf-cache"}
```

It then starts at night (`lora_hour`, default 2) once the machine has been idle (`lora_idle_minutes`), at most once per
`lora_interval_days` (7), never while the emergency halt is engaged. *Train now* in the page starts one immediately. The other keys
(`lora_min_approved`, `lora_epochs`, `lora_rank`, `lora_max_seq`, `lora_keep`, `lora_train_args`) are listed at the top of
`sdk/agent/lora_pipeline.py`. Setup (CUDA torch, bitsandbytes, the 3B, the converter) is in [INSTALL.md](../INSTALL.md).

## Safety

- Only approved items; blocked items cannot be approved; the filters run twice (queue, dataset build).
- `access_level: SECURED` nodes are never candidates and `compile_lora_adapter` refuses them.
- The emergency halt stops a run before anything starts (a halt that cannot be read counts as engaged), and the UI handlers refuse
  while halted.
- Refuses to run with `TEMP`, `TMP` or `HF_HOME` on C:.
- Never downloads: the base weights come from the local Hugging Face cache, the converter from a local checkout.

`nodes/system-ollama-adapter-deployment.html` still describes the Ollama `ADAPTER` procedure; it is out of date for this Ollama version.

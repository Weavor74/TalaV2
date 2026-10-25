# The voice adapter: training and deployment

Tala's voice adapter is a LoRA trained on **her own experience**: her chat sessions, and the HTML-NN nodes that hold her skills and
memories. It is trained and deployed automatically, on a schedule, with no human approval step. What stands in for a human glance is
that every stage must produce what the next one needs, the new model must give a sane answer before it is registered, and a failure at
any point registers nothing and cleans up after itself.

## What she trains on

`sdk/agent/real_usage_corpus.py` builds the corpus fresh each run, and `scripts/train_voice_adapter.py` trains on two sources by default:

| source | what is kept | what is left out (and counted) |
|---|---|---|
| **chat sessions** (`conversation_turns`, sessions named `chat-<time>`) | her own replies, with her `<thinking>` removed and the emotion label re-derived from the astro engine for that moment | tool narration, code, markup, bare tool calls, prompt leaks, fragments, duplicates, anything the safety filter rejects |
| **HTML-NN skills and memories** (`nodes/`) | `skill` nodes, `learning.*` lessons, `memory.*` remembered facts | generated documentation, settings, tool manuals, persona/identity nodes, the dream journal, failure-recovery notes, `access_level: SECURED` nodes at any depth |

HTML-NN is where she keeps what she knows, so it is a source, not an afterthought. The 522 synthetic stories are used only with
`--sources all`. Everything goes through the same exclude-never-redact safety filter (secrets and PII are dropped, not masked).
On the real data at the time of writing: 188 chat replies and 16 skill/memory nodes.

## The chain

```
corpus sync -> train -> merge -> convert to GGUF -> import into Ollama -> smoke test -> register -> prune -> record
```

1. **Corpus.** Counts what is there and what is new since the last training. A first training needs `lora_min_total` examples
   (100); another needs `lora_min_new` new ones (50). Too little is a skip, not a failure.
2. **Train.** `train_voice_adapter.py --train` on the base model (default `unsloth/Llama-3.2-1B-Instruct`, Meta, US-origin), on CPU,
   at below-normal priority so it does not take the machine from its user. About five hours for a full run.
3. **Merge.** `scripts/merge_adapter.py` folds the adapter into its base (offline, from the local Hugging Face cache).
4. **Convert.** llama.cpp's `convert_hf_to_gguf.py` writes a Q8_0 GGUF.
5. **Import.** `ollama create tala-voice-<run id>` from a Modelfile whose chat template is borrowed from an installed Llama 3 model.
6. **Smoke test.** The new model must give a sane answer (some words, no error text, not one word on repeat) or it is removed and
   never registered. This is not a quality judgement; it catches a model that is broken.
7. **Register and prune.** The model becomes a local provider (same id and config the server's own auto-detect would use), and the
   newest `lora_keep` (2) versions are kept so there is a way back. A locked provider row is never pruned.
8. **Record.** Every run, kept or not, is written as an HTML-NN record node (`system.lora-run`). It is documentation of what the
   system did, not something she learned, so it is not a training source.

## Why it goes through a merge and a GGUF

Measured on this machine (Ollama 0.40.6, Windows), not assumed:

* `ADAPTER <adapter>` in a Modelfile fails: *LoRA adapters are no longer supported*.
* `FROM <safetensors dir>` fails: *MLX runtime is not available* (this Ollama imports safetensors through Apple's MLX).
* A merged model converted to GGUF imports in seconds and answers. That is the only route in, so it is the pipeline.

`nodes/system-ollama-adapter-deployment.html` still describes the `ADAPTER` procedure; it is out of date for this Ollama version.

## What it does not do (v1)

* **It does not replace her main model.** The main model on Kagura is gpt-oss-20b; a LoRA on a 1B must not silently take over. By
  default the new model is registered at the **end of the local band**, exactly where the server would put any new Ollama model.
  `lora_promote: "primary"` is the opt-in to put it first.
* **No embedded-engine (`--lora`) target.** The embedded main is a 20B that cannot take a LoRA on a 6 GB card, and the original plan's
  Qwen base is excluded by the US-origin rule. If an embedded Llama-3.2 model is ever the main one, that target can be added.
* **It does not train per node or per turn.** That older design (`python/inference/lora_trainer.py`) is retired and stays off.

## Turning it on

It is **off** until you switch it on, because a run is hours of CPU. In `learning.json` (the learning settings file in the data root):

```json
{ "lora_auto_train": true }
```

Then, at night once the system has been idle, and only if enough is new, it starts. At most one attempt a day, whatever the outcome.
Other settings (all optional) are listed at the top of `sdk/agent/lora_pipeline.py`. The converter must be found: set `llama_cpp_src`
(or `TALA_LLAMA_CPP_SRC`) to a llama.cpp checkout, ideally the commit your engine binary was built from.

To run one now: the `compile_lora_adapter` tool with `now=true`, or from Python
`await lora_pipeline.run_pipeline(reason="manual", force=True)`. `lora_pipeline.status()` says what it last did, and the engine panel
shows it.

## Safety

* `access_level: SECURED` nodes are refused by the `compile_lora_adapter` tool and skipped by the corpus builder.
* The emergency halt stops a run before anything starts. A halt that cannot be read counts as engaged.
* Refuses to run if `TEMP`, `TMP` or `HF_HOME` is on `C:`.
* Never downloads: the base comes from the local cache, the converter from a local checkout, the chat template from an installed model.

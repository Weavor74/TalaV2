#!/usr/bin/env python
"""
scripts/train_voice_adapter.py

Train a LoRA adapter on Tala's voice from the pre-Weavor story corpus.

WHY THIS EXISTS
---------------
JIT LoRA training was never implemented. _perform_training_sync wrote
DUMMY_LORA_WEIGHTS_FOR_<node_id> when peft was missing and executed a bare
`pass` when it was present, while the queue logged "Successfully trained LoRA"
over the top of both. Every adapter in the repository is 37-51 bytes of ASCII.

WHAT IT TRAINS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
Voice and behaviour. A LoRA fitted to a few kilobytes of one node's prose will
overfit and teach cadence, register and habit -- which is exactly what is wanted
here and exactly why it must not be aimed at facts. Factual recall is the
retrieval path's job, and retrieval is better at it at every scale. The node
schema already draws this line: weight_behavior_influence is the axis this
serves.

The corpus is built for it. Each story carries turns marked
data-speaker="tala", each opening with the literal "[Emotional State: X]"
prefix that python/harness/memory.py writes into RP conversation memory, and
some carrying tool calls inline. Training on that surface form keeps voice,
emotion and tool habit on one axis rather than three.

Before reading the corpus it refreshes real-usage/ (Tala's own replies in the renderer's chat sessions) and learning-events/
(her HTML-NN skills and memories: the skill, lesson and remembered-fact nodes that use creates) from the live database and
nodes -- see sdk/agent/real_usage_corpus.py for what is kept and why. Those two are what she trains on by default; the
synthetic stories are used only with --sources all. Pass --no-real-usage to train on what is already on disk.

USAGE
    python scripts/train_voice_adapter.py --dry-run      # show the dataset, no torch needed
    python scripts/train_voice_adapter.py --train
    python scripts/train_voice_adapter.py --train --base unsloth/Llama-3.2-1B-Instruct

Training needs peft, trl, transformers and torch:
    uv pip install peft trl transformers accelerate
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CORPUS = REPO_ROOT / "data" / "training_corpus"
DEFAULT_OUTPUT = REPO_ROOT / "lora-generations" / "voice-adapter"

# Llama 3.2 1B Instruct (Meta, US-origin) -- same family as the embedded
# engine's default GGUF (Llama 3.2 3B), small enough to train on CPU. The
# unsloth mirror is the ungated copy of meta-llama/Llama-3.2-1B-Instruct.
# Project rule: US-origin base models only.
DEFAULT_BASE_MODEL = "unsloth/Llama-3.2-1B-Instruct"

# Attributes are pulled from the captured tag rather than matched in sequence:
# a greedy [^>]* between them swallows data-emotion before an optional group can
# see it, which silently reported every turn as having no emotion.
_TURN_RE = re.compile(r'<div class="turn"(?P<attrs>[^>]*)>(?P<body>.*?)</div>', re.S)
_ATTR_RE = re.compile(r'data-(?P<name>speaker|emotion)="(?P<value>[^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
_TOOL_CALL_RE = re.compile(r'<pre class="tool-call"[^>]*>(.*?)</pre>', re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", fragment)).strip()


#: What she trains on by default: her real chat sessions and her HTML-NN skills and memories. The synthetic stories are opt-in.
REAL_ERAS = ("real-usage", "learning-events")


def extract_turns(corpus_dir: Path, eras: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    """
    Every Tala turn in the corpus (or in the given ``eras``), as a training example.

    Only her turns: training on the interlocutor's lines would teach her to
    write them. The "[Emotional State: X]" prefix is preserved verbatim rather
    than stripped -- it is the exact surface form memory.py writes, and an
    adapter trained without it produces output that does not match its own
    memory format.
    """
    examples: list[dict[str, str]] = []
    for path in sorted(corpus_dir.rglob("*.html")):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        era_match = re.search(r'data-era="([^"]*)"', raw)
        era = era_match.group(1) if era_match else "unknown"
        if eras is not None and era not in eras:
            continue

        previous = None  # the user turn directly before a Tala turn: the message she is answering
        for match in _TURN_RE.finditer(raw):
            attrs = {m.group("name"): m.group("value") for m in _ATTR_RE.finditer(match.group("attrs"))}
            if attrs.get("speaker") == "user":
                previous = _text(match.group("body"))
                continue
            if attrs.get("speaker") != "tala":
                previous = None
                continue
            body = match.group("body")
            tool_calls = [_text(t) for t in _TOOL_CALL_RE.findall(body)]
            prose = _text(_TOOL_CALL_RE.sub(" ", body))
            if not prose:
                continue
            examples.append({
                "source": path.name,
                "era": era,
                "emotion": attrs.get("emotion", ""),
                "text": prose,
                "tool_calls": " ".join(tool_calls),
                "prompt": previous or "",
            })
            previous = None
    return examples


def limit_examples(examples: list[dict[str, str]], maximum: int | None) -> list[dict[str, str]]:
    """The last ``maximum`` examples (the corpus is written oldest first, so these are the newest), or all of them. For a smoke run."""
    return examples if not maximum or maximum >= len(examples) else examples[-maximum:]


def to_training_text(example: dict[str, str]) -> str:
    """One example as the model will see it, tool call included where present."""
    text = example["text"]
    if example["tool_calls"]:
        text = f"{text}\n{example['tool_calls']}"
    return text


def cmd_dry_run(examples: list[dict[str, str]]) -> int:
    if not examples:
        print("No Tala turns found. Has the corpus been generated?")
        return 1

    from collections import Counter
    eras = Counter(e["era"] for e in examples)
    emotions = Counter(e["emotion"] for e in examples if e["emotion"])
    with_tools = sum(1 for e in examples if e["tool_calls"])
    chars = sum(len(to_training_text(e)) for e in examples)

    print(f"examples          {len(examples)}")
    print(f"total characters  {chars}  (~{chars // 4} tokens)")
    print(f"turns with a tool call  {with_tools}")
    print(f"eras              {dict(eras)}")
    print(f"emotions          {dict(emotions)}")
    print()
    print("first two examples as the model will see them:")
    for example in examples[:2]:
        print(f"  --- {example['source']} [{example['era']}/{example['emotion']}]")
        print(f"      {to_training_text(example)[:220]}")

    if len(examples) < 50:
        print()
        print(f"WARNING: {len(examples)} examples is thin for a voice adapter. Generate more "
              f"with scripts/generate_stories.py before training anything you intend to keep.")
    return 0


def _ids(encoded) -> list[int]:
    """Token ids from whatever apply_chat_template returned: a plain list, or (transformers 5) a mapping carrying input_ids."""
    if hasattr(encoded, "keys"):
        encoded = encoded["input_ids"]
    if encoded and isinstance(encoded[0], list):
        encoded = encoded[0]
    return [int(i) for i in encoded]


def encode_example(tokenizer, example: dict[str, str], max_seq: int) -> dict[str, list[int]] | None:
    """
    One example as tokens and labels. With a message to answer it goes through the chat template and only her reply is scored (the
    message is context, not something to learn to say); without one it is plain text, scored whole, as the earlier trainer did.
    Cut to ``max_seq``. None when the message alone fills the limit, leaving nothing of hers to score.
    """
    prompt = (example.get("prompt") or "").strip()
    reply = to_training_text(example)
    if not prompt:
        encoded = tokenizer(reply, truncation=True, max_length=max_seq)
        ids = list(encoded["input_ids"])
        return {"input_ids": ids, "attention_mask": list(encoded["attention_mask"]), "labels": list(ids)}
    user = [{"role": "user", "content": prompt}]
    prefix = _ids(tokenizer.apply_chat_template(user, add_generation_prompt=True, tokenize=True))
    full = _ids(tokenizer.apply_chat_template(user + [{"role": "assistant", "content": reply}], tokenize=True))
    if len(prefix) >= max_seq or len(full) <= len(prefix):
        return None
    full = full[:max_seq]
    labels = [-100] * len(prefix) + full[len(prefix):]
    return {"input_ids": full, "attention_mask": [1] * len(full), "labels": labels}


def resolve_quantize(mode: str, *, cuda: bool, bnb: bool) -> str:
    """"4bit" (QLoRA) or "none". auto means 4-bit wherever a GPU and bitsandbytes exist; asking for it without them is an error."""
    if mode == "none":
        return "none"
    if mode == "auto":
        return "4bit" if cuda and bnb else "none"
    if not (cuda and bnb):
        raise SystemExit("--quantize 4bit needs a CUDA GPU and bitsandbytes (uv pip install bitsandbytes, with a CUDA build of torch)")
    return "4bit"


def target_modules(which: str) -> list[str]:
    """The layers the adapter touches: attention only (small, voice), or every linear layer (what the QLoRA persona adapter uses)."""
    if which == "all":
        return ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    return ["q_proj", "v_proj"]


def latest_checkpoint(output: Path) -> Path | None:
    """The newest ``checkpoints/checkpoint-N`` under ``output``, for resuming an interrupted run."""
    found = []
    for path in (output / "checkpoints").glob("checkpoint-*") if (output / "checkpoints").is_dir() else []:
        suffix = path.name.split("-", 1)[1]
        if path.is_dir() and suffix.isdigit():
            found.append((int(suffix), path))
    return max(found)[1] if found else None


def write_report(output: Path, **fields) -> None:
    """What was trained and what it cost, next to the adapter, for the pipeline and for a person to read."""
    import json

    output.mkdir(parents=True, exist_ok=True)
    (output / "train_report.json").write_text(json.dumps(fields, indent=2), encoding="utf-8")


def cmd_train(examples: list[dict[str, str]], base_model: str, output: Path, epochs: int, rank: int, *, quantize: str = "auto",
              target: str = "attn", max_seq: int = 512, resume: bool = False, keep_checkpoints: bool = False,
              learning_rate: float = 2e-4, accumulation: int = 4) -> int:
    """
    Train for real, or say plainly that it cannot.

    No placeholder is written here. Writing one is what let the previous
    implementation pass every existence check downstream while containing
    nothing.
    """
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            DataCollatorForSeq2Seq,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        print(f"Cannot train: {exc.name} is not installed.")
        print("  uv pip install peft trl transformers accelerate")
        print("Nothing was written. The adapter path is unchanged.")
        return 2

    if not examples:
        print("Refusing to train on an empty corpus.")
        return 1

    import importlib.util
    import shutil
    import time

    cuda = torch.cuda.is_available()
    mode = resolve_quantize(quantize, cuda=cuda, bnb=importlib.util.find_spec("bitsandbytes") is not None)
    print(f"base model   {base_model}")
    print(f"examples     {len(examples)}")
    print(f"lora rank    {rank}  ({target} layers)")
    print(f"precision    {mode if mode == '4bit' else ('bf16' if cuda else 'fp32')}  max sequence {max_seq}")
    print(f"output       {output}")

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    encoded = [e for e in (encode_example(tokenizer, ex, max_seq) for ex in examples) if e is not None]
    if len(encoded) < len(examples):
        print(f"skipped {len(examples) - len(encoded)} example(s) whose message alone fills {max_seq} tokens")
    if not encoded:
        print("Nothing left to train on.")
        return 1
    dataset = Dataset.from_list(encoded)

    started = time.perf_counter()
    if mode == "4bit":
        quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                                   bnb_4bit_use_double_quant=True)
        model = AutoModelForCausalLM.from_pretrained(base_model, quantization_config=quant, device_map={"": 0}, dtype=torch.bfloat16)
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    else:
        model = AutoModelForCausalLM.from_pretrained(base_model, dtype=torch.bfloat16 if cuda else torch.float32)
    model = get_peft_model(model, LoraConfig(
        r=rank,
        lora_alpha=rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=target_modules(target),
    ))
    model.print_trainable_parameters()

    output.mkdir(parents=True, exist_ok=True)
    checkpoint = latest_checkpoint(output) if resume else None
    if checkpoint:
        print(f"resuming from {checkpoint}")
    if cuda:
        torch.cuda.reset_peak_memory_stats()
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(output / "checkpoints"),
            num_train_epochs=epochs,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=accumulation,
            learning_rate=learning_rate,
            logging_steps=5,
            save_strategy="steps",
            save_steps=25,
            save_total_limit=2,
            report_to=[],
            bf16=cuda,
            optim="paged_adamw_8bit" if mode == "4bit" else "adamw_torch",
            gradient_checkpointing=mode == "4bit",
            gradient_checkpointing_kwargs={"use_reentrant": False} if mode == "4bit" else None,
        ),
        train_dataset=dataset,
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100, pad_to_multiple_of=8),
    )
    trainer.train(resume_from_checkpoint=str(checkpoint) if checkpoint else None)
    model.save_pretrained(str(output))
    losses = [h["loss"] for h in trainer.state.log_history if "loss" in h]
    write_report(output, base=base_model, examples=len(encoded), rank=rank, quantize=mode, seconds=round(time.perf_counter() - started, 1),
                 peak_vram_mb=round(torch.cuda.max_memory_allocated() / 2**20) if cuda else 0,
                 final_loss=round(losses[-1], 4) if losses else None, steps=int(trainer.state.global_step))

    written = sorted(p.name for p in output.glob("*") if p.is_file())
    print(f"\nwrote {output}: {written}")
    adapter = output / "adapter_model.safetensors"
    if adapter.exists():
        size = adapter.stat().st_size
        print(f"adapter_model.safetensors  {size} bytes")
        if size < 4096:
            print("WARNING: that is placeholder-sized. Something did not train.")
            return 1
    if not keep_checkpoints:
        shutil.rmtree(output / "checkpoints", ignore_errors=True)  # only needed to resume; a finished adapter does not need them
    return 0


def sync_real_usage() -> None:
    """
    Refresh data/training_corpus/real-usage and learning-events from Tala's real chats and lesson nodes, then say what was
    kept and why the rest was not. Best effort: a database that is down costs the run its real-usage half and nothing else.
    """
    import asyncio

    from python.harness import db
    from sdk.agent import real_usage_corpus as ruc

    async def run() -> tuple[dict, dict]:
        try:
            await db.init_pool()
        except Exception as exc:
            print(f"real-usage: database unavailable ({exc}); conversation turns not refreshed")
        try:
            return await ruc.sync_real_usage_corpus(), await ruc.sync_learning_events_corpus()
        finally:
            try:
                await db.close_pool()
            except Exception:
                pass

    chats, events = asyncio.run(run())
    if chats.get("success"):
        why = ", ".join(f"{n} {reason}" for reason, n in sorted(chats["excluded_by"].items(), key=lambda kv: -kv[1])) or "none"
        print(f"real-usage: kept {chats['included']} of {chats['scanned']} chat replies (left out: {why})")
    else:
        print(f"real-usage: conversation turns not refreshed ({chats.get('error')})")
    if events.get("success"):
        print(f"real-usage: kept {events['included']} of {events['scanned']} skill and memory nodes")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--base", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--sources", choices=("real", "all"), default=None,
                    help="real = her chat sessions and HTML-NN skills/memories only; all = also the synthetic stories. "
                         "Default: real for the shared corpus, everything for a --corpus directory you point at")
    ap.add_argument("--no-real-usage", action="store_true",
                    help="skip refreshing the real-usage and learning-event corpora from the live database and nodes")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--max-examples", type=int, default=None,
                    help="train on only the newest N examples: a quick smoke run of the whole pipeline, not a real adapter")
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--quantize", choices=("auto", "4bit", "none"), default="auto",
                    help="4bit = QLoRA (needs a CUDA GPU and bitsandbytes); auto picks it where available")
    ap.add_argument("--target", choices=("attn", "all"), default="attn", help="LoRA on attention only, or on every linear layer")
    ap.add_argument("--max-seq", type=int, default=512, help="tokens per example (1024 is the most a 6 GB card holds for a 3B)")
    ap.add_argument("--resume", action="store_true", help="continue from the newest checkpoint under --output, if there is one")
    ap.add_argument("--keep-checkpoints", action="store_true")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="inspect the dataset; no training deps needed")
    group.add_argument("--train", action="store_true")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not args.no_real_usage and corpus.resolve() == DEFAULT_CORPUS.resolve():
        sync_real_usage()  # only into the default corpus: the sync writes there, and a custom directory is the caller's own
    if not corpus.is_dir():
        print(f"Corpus directory not found: {corpus}")
        return 2

    use_real_only = args.sources == "real" or (args.sources is None and corpus.resolve() == DEFAULT_CORPUS.resolve())
    examples = limit_examples(extract_turns(corpus, REAL_ERAS if use_real_only else None), args.max_examples)
    if args.dry_run:
        return cmd_dry_run(examples)
    return cmd_train(examples, args.base, Path(args.output), args.epochs, args.rank, quantize=args.quantize, target=args.target,
                     max_seq=args.max_seq, resume=args.resume, keep_checkpoints=args.keep_checkpoints)


if __name__ == "__main__":
    raise SystemExit(main())

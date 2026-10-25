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

        for match in _TURN_RE.finditer(raw):
            attrs = {m.group("name"): m.group("value") for m in _ATTR_RE.finditer(match.group("attrs"))}
            if attrs.get("speaker") != "tala":
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
            })
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


def cmd_train(examples: list[dict[str, str]], base_model: str, output: Path,
              epochs: int, rank: int) -> int:
    """
    Train for real, or say plainly that it cannot.

    No placeholder is written here. Writing one is what let the previous
    implementation pass every existence check downstream while containing
    nothing.
    """
    try:
        import torch  # noqa: F401
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            DataCollatorForLanguageModeling,
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

    print(f"base model   {base_model}")
    print(f"examples     {len(examples)}")
    print(f"lora rank    {rank}")
    print(f"output       {output}")

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dataset = Dataset.from_dict({"text": [to_training_text(e) for e in examples]})

    def tokenize(batch):
        return tokenizer(batch["text"], truncation=True, max_length=512)

    dataset = dataset.map(tokenize, batched=True, remove_columns=["text"])

    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
    )
    # Attention projections only: voice lives in how attention is distributed,
    # and restricting the target keeps the adapter small enough to be worth
    # swapping per turn.
    model = get_peft_model(model, LoraConfig(
        r=rank,
        lora_alpha=rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "v_proj"],
    ))
    model.print_trainable_parameters()

    output.mkdir(parents=True, exist_ok=True)
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(output / "checkpoints"),
            num_train_epochs=epochs,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            learning_rate=2e-4,
            logging_steps=5,
            save_strategy="no",
            report_to=[],
            fp16=torch.cuda.is_available(),
        ),
        train_dataset=dataset,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
    )
    trainer.train()
    model.save_pretrained(str(output))

    written = sorted(p.name for p in output.glob("*") if p.is_file())
    print(f"\nwrote {output}: {written}")
    adapter = output / "adapter_model.safetensors"
    if adapter.exists():
        size = adapter.stat().st_size
        print(f"adapter_model.safetensors  {size} bytes")
        if size < 4096:
            print("WARNING: that is placeholder-sized. Something did not train.")
            return 1
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
    return cmd_train(examples, args.base, Path(args.output), args.epochs, args.rank)


if __name__ == "__main__":
    raise SystemExit(main())

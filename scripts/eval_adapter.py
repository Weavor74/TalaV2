#!/usr/bin/env python
"""
scripts/eval_adapter.py

Held-out loss for the plain base model and one or more LoRA adapters, on the examples the dataset held out (never trained on).
This is the "persona" half of the eval gate (sdk/agent/lora_eval.py): the lower an adapter's loss on replies it has not seen, the
more like her it has become. Only her reply is scored, exactly as in training (scripts/train_voice_adapter.py's encode_example).

    python scripts/eval_adapter.py --base unsloth/Llama-3.2-3B-Instruct --heldout <dataset>/heldout \\
        --adapter current=<peft dir> --adapter candidate=<peft dir> --out scores.json

The base is loaded once (4-bit on the GPU where bitsandbytes and CUDA exist), each adapter is loaded beside it and switched on in turn.
The result is JSON: ``{"base": {"nll", "tokens", "examples"}, "<name>": {...}}``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def mean_nll(pairs: list[tuple[float, int]]) -> float | None:
    """Total loss over total scored tokens. None when nothing was scored: that is not a loss of zero."""
    tokens = sum(n for _, n in pairs)
    return sum(loss for loss, _ in pairs) / tokens if tokens else None


def parse_adapter(spec: str) -> tuple[str, str]:
    name, sep, path = spec.partition("=")
    if not sep or not name or not path:
        raise SystemExit(f"--adapter wants NAME=PATH, not {spec!r}")
    if name == "base":
        raise SystemExit("the name 'base' is the plain model; give the adapter another name")
    return name, path


def summarise(per_example: dict[str, list[tuple[float, int]]]) -> dict[str, dict]:
    return {name: {"nll": mean_nll(pairs), "tokens": sum(n for _, n in pairs), "examples": len(pairs)} for name, pairs in per_example.items()}


def _score(model, tokenizer, examples, max_seq: int, device) -> list[tuple[float, int]]:
    import torch

    from scripts.train_voice_adapter import encode_example

    pairs: list[tuple[float, int]] = []
    model.eval()
    for example in examples:
        encoded = encode_example(tokenizer, example, max_seq)
        if encoded is None:
            continue
        ids = torch.tensor([encoded["input_ids"]], device=device)
        labels = torch.tensor([encoded["labels"]], device=device)
        with torch.no_grad():
            logits = model(input_ids=ids, attention_mask=torch.ones_like(ids)).logits
        shift_logits, shift_labels = logits[:, :-1, :].float(), labels[:, 1:]
        mask = shift_labels != -100
        if not mask.any():
            continue
        loss = torch.nn.functional.cross_entropy(shift_logits[mask], shift_labels[mask], reduction="sum")
        pairs.append((float(loss), int(mask.sum())))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--heldout", required=True, help="a directory of HTML corpus pages (the dataset's heldout/)")
    ap.add_argument("--adapter", action="append", default=[], help="NAME=PEFT_DIR, repeatable")
    ap.add_argument("--max-seq", type=int, default=768)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    adapters = [parse_adapter(a) for a in args.adapter]

    import importlib.util

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    from scripts.train_voice_adapter import extract_turns, resolve_quantize

    examples = extract_turns(Path(args.heldout))
    if not examples:
        print(f"no held-out examples under {args.heldout}")
        return 1
    cuda = torch.cuda.is_available()
    mode = resolve_quantize("auto", cuda=cuda, bnb=importlib.util.find_spec("bitsandbytes") is not None)
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if mode == "4bit":
        quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                                   bnb_4bit_use_double_quant=True)
        model = AutoModelForCausalLM.from_pretrained(args.base, quantization_config=quant, device_map={"": 0}, dtype=torch.bfloat16)
    else:
        model = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.bfloat16 if cuda else torch.float32)
    device = next(model.parameters()).device

    per_example: dict[str, list[tuple[float, int]]] = {}
    if adapters:
        from peft import PeftModel

        first_name, first_path = adapters[0]
        model = PeftModel.from_pretrained(model, first_path, adapter_name=first_name)
        for name, path in adapters[1:]:
            model.load_adapter(path, adapter_name=name)
        with model.disable_adapter():
            per_example["base"] = _score(model, tokenizer, examples, args.max_seq, device)
        for name, _ in adapters:
            model.set_adapter(name)
            per_example[name] = _score(model, tokenizer, examples, args.max_seq, device)
    else:
        per_example["base"] = _score(model, tokenizer, examples, args.max_seq, device)

    summary = summarise(per_example)
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("RESULT " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

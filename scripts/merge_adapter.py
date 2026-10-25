"""
scripts/merge_adapter.py -- fold a trained LoRA adapter into its base model: a plain model directory comes out.

    python scripts/merge_adapter.py <adapter-dir> <output-dir> [--base <model-dir-or-hf-id>]

A step of the train -> deploy pipeline (sdk/agent/lora_pipeline.py). It exists because this machine's Ollama refuses an
``ADAPTER`` ("LoRA adapters are no longer supported") and cannot import safetensors on Windows (no MLX), so the only way in is a
merged model converted to GGUF. Everything is offline: the base is read from the adapter's own config and found in the local
Hugging Face cache, never downloaded. Nothing is written on a failed merge.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

#: An adapter smaller than this is a placeholder, not a trained model (a real r=8 q/v adapter on a 1B is ~3 MB).
MIN_ADAPTER_BYTES = 4096
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "generation_config.json", "chat_template.jinja")


def base_of(adapter_dir: Path) -> str:
    """The base model the adapter was trained against, from its own adapter_config.json."""
    config = json.loads((Path(adapter_dir) / "adapter_config.json").read_text(encoding="utf-8"))
    base = config.get("base_model_name_or_path")
    if not base:
        raise ValueError(f"{adapter_dir}/adapter_config.json does not name its base model")
    return str(base)


def _local_base_dir(base: str) -> Path:
    """A directory holding the base model: the path itself, or the local Hugging Face snapshot of that repo id."""
    if Path(base).is_dir():
        return Path(base)
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(base, local_files_only=True))
    except Exception as exc:
        raise RuntimeError(f"the base model {base!r} is not in the local Hugging Face cache, and merging never downloads: {exc}") from exc


def merge(adapter_dir: Path, output_dir: Path, base: str | Path | None = None) -> Path:
    """Merge ``adapter_dir`` into its base and write the result to ``output_dir``. Returns ``output_dir``."""
    adapter_dir, output_dir = Path(adapter_dir), Path(output_dir)
    weights = adapter_dir / "adapter_model.safetensors"
    if not (adapter_dir / "adapter_config.json").is_file() or not weights.is_file():
        raise FileNotFoundError(f"{adapter_dir} is not a trained adapter (no adapter_config.json / adapter_model.safetensors)")
    if weights.stat().st_size < MIN_ADAPTER_BYTES:
        raise ValueError(f"{weights} is {weights.stat().st_size} bytes: that is a placeholder, not trained weights")
    base_dir = _local_base_dir(str(base) if base else base_of(adapter_dir))

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM

    started = time.monotonic()
    # float32 for the merge itself, bfloat16 on disk (the base's own dtype): adding the low-rank update in half precision loses it.
    model = AutoModelForCausalLM.from_pretrained(str(base_dir), dtype=torch.float32)
    model = PeftModel.from_pretrained(model, str(adapter_dir)).merge_and_unload().to(torch.bfloat16)

    # Written to a sibling temp directory and moved into place, so a crash never leaves a half-written model where one is expected.
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output_dir.name + ".partial-", dir=output_dir.parent))
    try:
        model.save_pretrained(str(staging), safe_serialization=True)
        for name in TOKENIZER_FILES:
            if (base_dir / name).is_file():
                shutil.copy2(base_dir / name, staging / name)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(f"merged {adapter_dir.name} into {base_dir.name} in {time.monotonic() - started:.0f}s -> {output_dir}", flush=True)
    return output_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adapter_dir")
    ap.add_argument("output_dir")
    ap.add_argument("--base", default=None, help="a model directory or Hugging Face id; default: the adapter's own base")
    args = ap.parse_args(argv)
    try:
        merge(Path(args.adapter_dir), Path(args.output_dir), args.base)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"merge failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

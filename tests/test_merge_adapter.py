"""
scripts/merge_adapter.py -- fold a trained LoRA adapter into its base model so the result is a plain model directory.

This is a step of the train -> deploy pipeline because this Ollama build refuses `ADAPTER` ("LoRA adapters are no longer
supported") and cannot import safetensors on Windows (no MLX), so the only route in is a merged model converted to GGUF. The
tests use a tiny randomly initialised Llama built here, so the maths is checked for real without any download.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
transformers = pytest.importorskip("transformers")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_adapter.py"


@pytest.fixture(scope="module")
def merge_module():
    spec = importlib.util.spec_from_file_location("merge_adapter_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_adapter_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """A base model, and an adapter whose weights are deliberately NOT zero so a merge has something to change."""
    from peft import LoraConfig, get_peft_model
    from transformers import LlamaConfig, LlamaForCausalLM

    root = tmp_path_factory.mktemp("tiny")
    torch.manual_seed(0)
    config = LlamaConfig(vocab_size=64, hidden_size=64, intermediate_size=128, num_hidden_layers=2, num_attention_heads=4,
                         num_key_value_heads=4, max_position_embeddings=64)
    base = LlamaForCausalLM(config)
    base_dir = root / "base"
    base.save_pretrained(base_dir)
    (base_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    peft_model = get_peft_model(LlamaForCausalLM.from_pretrained(base_dir), LoraConfig(r=8, lora_alpha=16, target_modules=["q_proj", "v_proj"]))
    for name, param in peft_model.named_parameters():
        if "lora_B" in name:
            torch.nn.init.normal_(param, std=0.5)  # PEFT initialises B to zero; a trained adapter is not zero
    adapter_dir = root / "adapter"
    peft_model.save_pretrained(adapter_dir)
    cfg = json.loads((adapter_dir / "adapter_config.json").read_text(encoding="utf-8"))
    cfg["base_model_name_or_path"] = str(base_dir)
    (adapter_dir / "adapter_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return base_dir, adapter_dir, root


def test_the_merged_model_is_plain_and_carries_the_adapters_effect(merge_module, tiny):
    from transformers import AutoModelForCausalLM

    base_dir, adapter_dir, root = tiny
    out = root / "merged"
    merge_module.merge(adapter_dir, out, base=base_dir)

    assert (out / "model.safetensors").is_file() and (out / "config.json").is_file()
    assert not (out / "adapter_config.json").exists(), "a merged model must not still look like an adapter"
    assert (out / "tokenizer.json").is_file(), "the tokenizer files travel with the weights"

    base = AutoModelForCausalLM.from_pretrained(base_dir, dtype=torch.float32)
    merged = AutoModelForCausalLM.from_pretrained(out, dtype=torch.float32)
    q_base = base.model.layers[0].self_attn.q_proj.weight
    q_merged = merged.model.layers[0].self_attn.q_proj.weight
    assert not torch.allclose(q_base, q_merged, atol=1e-3), "the adapter changed nothing"
    # layers the adapter does not target are untouched (up to the bfloat16 the merged copy is stored in)
    assert torch.allclose(base.model.layers[0].mlp.up_proj.weight, merged.model.layers[0].mlp.up_proj.weight, atol=2e-2)


def test_the_base_is_found_from_the_adapters_own_config_when_not_given(merge_module, tiny):
    base_dir, adapter_dir, root = tiny
    assert merge_module.base_of(adapter_dir) == str(base_dir)


def test_a_missing_adapter_is_an_error_not_an_empty_model(merge_module, tmp_path):
    with pytest.raises(FileNotFoundError):
        merge_module.merge(tmp_path / "nothing-here", tmp_path / "out")


def test_a_placeholder_sized_adapter_is_refused(merge_module, tiny, tmp_path):
    base_dir, adapter_dir, _root = tiny
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "adapter_config.json").write_text((adapter_dir / "adapter_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    (fake / "adapter_model.safetensors").write_bytes(b"x" * 64)
    with pytest.raises(ValueError, match="placeholder"):
        merge_module.merge(fake, tmp_path / "out", base=base_dir)


def test_the_merge_runs_offline_and_never_downloads(merge_module, tmp_path, monkeypatch):
    """The base is resolved from the local Hugging Face cache only; an uncached base is a clear error."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cfg = tmp_path / "adapter"
    cfg.mkdir()
    (cfg / "adapter_config.json").write_text(json.dumps({"base_model_name_or_path": "nobody/not-a-real-model"}), encoding="utf-8")
    (cfg / "adapter_model.safetensors").write_bytes(b"x" * 8192)
    with pytest.raises(RuntimeError, match="not in the local Hugging Face cache"):
        merge_module.merge(cfg, tmp_path / "out")

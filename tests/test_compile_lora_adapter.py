"""
compile_lora_adapter must never fabricate an adapter, and must never write into a node.

It used to hash the node's file name into 256 floats, wrap them in a safetensors header, write them into the node as
``lora-weights`` and return "Successfully compiled". Nothing was trained, so every block was noise that looked like a model and
was then validated, selected and broadcast as one. It then refused outright while LoRA was unimplemented.

Training is real now (sdk/agent/lora_pipeline.py) but it is not per node: one adapter is trained over her chat sessions and
HTML-NN skills and memories, on a schedule. So the tool reports on that, or starts a run, and what these tests protect is
unchanged: no fabricated weights, no writes into the node, SECURED nodes refused, a missing node reported.
"""
from __future__ import annotations

import pytest

from python.inference import lora_trainer
from sdk.agent import tools
from sdk.agent.tools import ToolRegistry, compile_lora_adapter

NODE = """<!DOCTYPE html>
<html>
<body>
<article data-node-id="test-compile-node" data-node-type="skill" data-execution-authority="0.20">
  <header><h1>Test Node</h1></header>
  <section id="semantic-memory"><p>Test description.</p></section>
</article>
</body>
</html>"""


@pytest.fixture
def node(tmp_path):
    path = tmp_path / "test-compile-node.html"
    path.write_text(NODE, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_it_never_writes_into_the_node_or_fabricates_weights(node):
    before = node.read_bytes()

    reply = await compile_lora_adapter(str(node), "task-1")

    assert node.read_bytes() == before, "the node must be exactly as it was"
    assert b'id="lora-weights"' not in node.read_bytes()
    assert "not per node" in reply.lower(), "it says what it did instead: adapters are not trained per node"


@pytest.mark.asyncio
async def test_a_missing_node_is_still_reported_as_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        await compile_lora_adapter(str(tmp_path / "nope.html"))


def test_the_tool_describes_what_it_now_does():
    entry = next(t for t in ToolRegistry.get_tools() if t["name"] == "compile_lora_adapter")
    text = entry["description"].lower()
    assert "html-nn" in text and "now=true" in text
    assert "safetensors binary" not in text, "the old description promised something it never did"


@pytest.mark.asyncio
async def test_the_helper_skips_while_lora_is_not_implemented(node, monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("compile_lora_adapter was called although LoRA is not implemented")

    before = node.read_bytes()
    monkeypatch.setattr(tools, "compile_lora_adapter", must_not_run)
    assert lora_trainer.IMPLEMENTED is False
    assert await tools.compile_adapter_if_implemented(str(node), "task-1") is False
    assert node.read_bytes() == before


@pytest.mark.asyncio
async def test_the_helper_never_compiles_per_node_even_if_the_old_flag_is_flipped(node, monkeypatch):
    """The goal engine, the dream and weight adjustment call this as the last step of real work. Flipping the retired queue's flag
    used to make each of them train; now training is scheduled, so none of them does."""
    async def must_not_run(*args, **kwargs):
        raise AssertionError("a per-node compile was started")

    monkeypatch.setattr(lora_trainer, "IMPLEMENTED", True)
    monkeypatch.setattr(tools, "compile_lora_adapter", must_not_run)
    assert await tools.compile_adapter_if_implemented(str(node), "task-1") is False

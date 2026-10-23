"""
compile_lora_adapter must not fabricate an adapter.

It used to hash the node's file name into 256 floats, wrap them in a
safetensors header, write them into the node as ``lora-weights`` and return
"Successfully compiled". Nothing was trained, and no engine loads an adapter, so
every block it produced was noise that looked like a model and was then
validated, selected and broadcast as one. LoRA is not implemented
(``lora_trainer.IMPLEMENTED``); the tool now says so and writes nothing.
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
async def test_it_refuses_instead_of_fabricating_weights(node):
    before = node.read_bytes()

    with pytest.raises(lora_trainer.LoraTrainingUnavailable, match="not implemented"):
        await compile_lora_adapter(str(node), "task-1")

    assert node.read_bytes() == before, "a refusal must leave the node exactly as it was"
    assert b'id="lora-weights"' not in node.read_bytes()


@pytest.mark.asyncio
async def test_a_missing_node_is_still_reported_as_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        await compile_lora_adapter(str(tmp_path / "nope.html"))


def test_the_tool_describes_itself_as_not_implemented():
    entry = next(t for t in ToolRegistry.get_tools() if t["name"] == "compile_lora_adapter")
    text = entry["description"].lower()
    assert "not implemented" in text
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
async def test_the_helper_compiles_once_lora_is_real(node, monkeypatch):
    calls = []

    async def fake_compile(path, task_id=None):
        calls.append((path, task_id))

    monkeypatch.setattr(lora_trainer, "IMPLEMENTED", True)
    monkeypatch.setattr(tools, "compile_lora_adapter", fake_compile)

    assert await tools.compile_adapter_if_implemented(str(node), "task-1") is True
    assert calls == [(str(node), "task-1")]

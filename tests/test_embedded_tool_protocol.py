"""
The embedded engine's tool-call protocol, and the single-engine status shape.

`<tool_call>{...}</tool_call>` is Qwen's own chat-template convention for tool
calls; the harness normalises it to `<call:name attr="v" />` so the turn loop
can dispatch without knowing which provider produced the text. That parsing is
provider-agnostic and unchanged by which engine renders the template -- it
used to be Transformers running the template in-process, now it's llama-server
(via --jinja) doing the same rendering out-of-process. Only the second half of
this file changed: three embedded engines collapsed to one real one
(llama.cpp), so the platform-support gate that used to matter (vLLM has no
Windows wheels) no longer applies -- there is nothing left to be unsupported.
"""
import pytest

from python.harness.server import (
    _find_tool_call,
    _parse_chat_template_tool_calls,
    _tool_calls_to_call_tags,
)
from python.inference.engine_manager import ENGINE_ID, get_all_engines, get_status


# --- <tool_call> parsing ----------------------------------------------------


def test_template_tool_call_becomes_a_dispatchable_tag():
    """The template asks for {"name":..., "arguments":...}; the harness dispatches
    on <call:/>. Normalising here means the embedded path rejoins the same
    protocol-agnostic loop as every other provider."""
    raw = 'Let me look.\n<tool_call>\n{"name": "web_search", "arguments": {"query": "webb telescope"}}\n</tool_call>'

    calls = _parse_chat_template_tool_calls(raw)
    tag = _tool_calls_to_call_tags(calls)
    match = _find_tool_call(tag)

    assert calls == [{"function": {"name": "web_search", "arguments": {"query": "webb telescope"}}}]
    assert match is not None
    assert match.group(1) == "web_search"


def test_multiple_tool_calls_are_all_parsed():
    raw = ('<tool_call>{"name": "a", "arguments": {}}</tool_call>'
           '<tool_call>{"name": "b", "arguments": {"x": 1}}</tool_call>')

    assert [c["function"]["name"] for c in _parse_chat_template_tool_calls(raw)] == ["a", "b"]


def test_missing_arguments_key_defaults_to_empty():
    """A small model routinely omits `arguments` for a no-parameter tool."""
    calls = _parse_chat_template_tool_calls('<tool_call>{"name": "harness.health"}</tool_call>')

    assert calls == [{"function": {"name": "harness.health", "arguments": {}}}]


def test_malformed_or_nameless_blocks_are_skipped():
    """Half-generated JSON is a normal failure mode at small model sizes. It must
    not become a <call:/> for a nonexistent tool, which would be reported as a
    tool failure rather than as no tool call."""
    assert _parse_chat_template_tool_calls('<tool_call>{"name": "x"</tool_call>') == []
    assert _parse_chat_template_tool_calls('<tool_call>{"arguments": {}}</tool_call>') == []
    assert _parse_chat_template_tool_calls('<tool_call>["not", "an", "object"]</tool_call>') == []
    assert _parse_chat_template_tool_calls("no tool calls here") == []
    assert _parse_chat_template_tool_calls("") == []


# --- single-engine status shape ---------------------------------------------


def test_exactly_one_engine_is_offered():
    """Three engines collapsed to one real one; nothing else should reappear
    in the list without a deliberate decision to add it back."""
    engines = get_all_engines()
    assert [e["id"] for e in engines] == [ENGINE_ID]


def test_the_one_engine_is_always_supported():
    """Unlike vLLM (no Windows wheels) or the old llama-cpp-python stub, the
    llama.cpp engine is a self-contained binary Tala fetches itself -- there is
    no platform it cannot be offered on."""
    status = get_status(ENGINE_ID)
    assert status["is_supported"] is True
    assert status["unsupported_reason"] is None


def test_unknown_engine_id_is_refused():
    with pytest.raises(ValueError):
        get_status("transformers")

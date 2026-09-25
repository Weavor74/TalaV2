"""
The embedded 0.5B's tool path, and the engine platform gate.

Two defects of the same family. The chat template that ships with
Qwen2.5-0.5B-Instruct supports tool calling natively, and `tools` was never
passed to it — so the smallest model received thousands of tokens of XML tag
documentation it cannot follow and no tool definitions it can. Separately, the
engine list offered vLLM on Windows, where it has no wheels, so the only signal
the user got was a pip build failing several minutes in.
"""
import sys
from unittest.mock import Mock

import pytest

from python.harness.server import (
    _find_tool_call,
    _parse_chat_template_tool_calls,
    _tool_calls_to_call_tags,
)
from python.inference.embedded_engine import _apply_template
from python.inference.engine_manager import (
    ENGINES,
    get_all_engines,
    _is_engine_supported,
    start_install,
    _unsupported_reason,
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}]


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
    """A 0.5B routinely omits `arguments` for a no-parameter tool."""
    calls = _parse_chat_template_tool_calls('<tool_call>{"name": "harness.health"}</tool_call>')

    assert calls == [{"function": {"name": "harness.health", "arguments": {}}}]


def test_malformed_or_nameless_blocks_are_skipped():
    """Half-generated JSON is the normal failure mode at this model size. It must
    not become a <call:/> for a nonexistent tool, which would be reported as a
    tool failure rather than as no tool call."""
    assert _parse_chat_template_tool_calls('<tool_call>{"name": "x"</tool_call>') == []
    assert _parse_chat_template_tool_calls('<tool_call>{"arguments": {}}</tool_call>') == []
    assert _parse_chat_template_tool_calls('<tool_call>["not", "an", "object"]</tool_call>') == []
    assert _parse_chat_template_tool_calls("no tool calls here") == []
    assert _parse_chat_template_tool_calls("") == []


# --- template rendering -----------------------------------------------------


def test_tools_are_passed_to_the_chat_template():
    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = "rendered"
    messages = [{"role": "user", "content": "hi"}]

    _apply_template(tokenizer, messages, TOOLS)

    assert tokenizer.apply_chat_template.call_args.kwargs["tools"] == TOOLS


def test_no_tools_argument_when_there_are_none():
    """Passing tools=None to a template that branches on `tools` would still take
    the tool branch in some revisions; omit the argument entirely instead."""
    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = "rendered"

    _apply_template(tokenizer, [{"role": "user", "content": "hi"}], None)

    assert "tools" not in tokenizer.apply_chat_template.call_args.kwargs


def test_template_that_rejects_tools_falls_back_instead_of_failing():
    """A non-Qwen embedded model, or an older template, may not understand
    `tools`. Losing tool calling is acceptable; losing generation is not."""
    tokenizer = Mock()
    tokenizer.apply_chat_template.side_effect = [TypeError("unexpected keyword 'tools'"), "rendered"]

    out = _apply_template(tokenizer, [{"role": "user", "content": "hi"}], TOOLS)

    assert out == "rendered"
    assert tokenizer.apply_chat_template.call_count == 2
    assert "tools" not in tokenizer.apply_chat_template.call_args.kwargs


# --- engine platform gate ---------------------------------------------------


def test_vllm_is_not_offered_on_windows():
    """vLLM publishes no Windows wheels. Offering it sends the user into a source
    build that fails on a missing CUDA toolchain."""
    assert ENGINES["vllm"]["platforms"] == ["linux"]
    assert _is_engine_supported("vllm") is (sys.platform == "linux")


def test_pure_python_engines_are_available_everywhere():
    for engine in ("transformers", "llamacpp"):
        assert _is_engine_supported(engine), f"{engine} should install on any platform"


def test_unsupported_engine_reports_status_and_reason_not_just_absence():
    """'not_installed' invites a click. 'unsupported' with a reason does not."""
    engines = {e["id"]: e for e in get_all_engines()}

    for engine in engines.values():
        assert "is_supported" in engine
        if not engine["is_supported"] and not engine["is_installed"]:
            assert engine["status"] == "unsupported"
            assert engine["unsupported_reason"]
        else:
            assert engine["unsupported_reason"] is None


@pytest.mark.skipif(sys.platform == "linux", reason="vLLM is genuinely installable on linux")
def test_installing_an_unsupported_engine_is_refused_up_front():
    """Backstop for a direct RPC call that bypasses the UI."""
    with pytest.raises(RuntimeError, match="Cannot install 'vllm'"):
        start_install("vllm")


def test_unsupported_reason_is_none_when_supported():
    assert _unsupported_reason("transformers") is None

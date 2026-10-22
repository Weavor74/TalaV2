"""
gpt-oss harmony output and native tool_calls, end to end through
generate_stream: tool calls come back native (at low reasoning with empty
text), reasoning is never shown, reasoning effort follows the turn type, and
raw harmony markers are cleaned up if they ever reach the text stream.
"""
from __future__ import annotations

import json
import types

import pytest

from python.inference import harmony
from python.inference import llama_server_manager as lsm
from python.inference import profiles


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}"


class _Resp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _Resp(self._lines)

    async def __aexit__(self, *a):
        return False


class _Client:
    sent: list[dict] = []

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None):
        _Client.sent.append(json)
        return _Ctx(self._lines)


@pytest.fixture(autouse=True)
def _engine(monkeypatch, tmp_path):
    for var in ("TALA_PROFILES_FILE", "TALA_ENGINE", "TALA_MODEL_PATH", "TALA_LLAMA_SERVER_BIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TALA_PROFILE", "kagura")
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.setenv("TALA_TOOLS_DIR", str(tmp_path / "t"))
    monkeypatch.setenv("TALA_MODELS_DIR", str(tmp_path / "m"))

    async def fake_ensure(preferred=None):
        return types.SimpleNamespace(variant="external", base_url="http://127.0.0.1:0")

    monkeypatch.setattr(lsm, "_ensure_running", fake_ensure)
    _Client.sent = []


def _stream(monkeypatch, lines):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(lines))


TOOLS = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]


# --- parsing ---------------------------------------------------------------------


def test_native_tool_calls_lose_the_functions_prefix_and_get_default_arguments():
    calls = harmony.normalize_tool_calls([
        {"function": {"name": "functions.get_time", "arguments": ""}},
        {"function": {"name": "web_search", "arguments": {"query": "x"}}},
        {"function": {"name": "", "arguments": "{}"}},
    ])
    assert calls == [
        {"name": "get_time", "arguments": "{}"},
        {"name": "web_search", "arguments": '{"query": "x"}'},
    ]


def test_raw_harmony_text_yields_final_text_and_calls_but_never_reasoning():
    raw = (
        "<|channel|>analysis<|message|>The user wants the time; I should call the tool.<|end|>"
        "<|start|>assistant<|channel|>commentary to=functions.get_time <|constrain|>json"
        '<|message|>{"zone": "UTC"}<|call|>'
    )
    visible, calls = harmony.split_harmony(raw)
    assert visible == ""
    assert calls == [{"name": "get_time", "arguments": '{"zone": "UTC"}'}]
    assert "user wants" not in visible


def test_raw_harmony_final_channel_is_the_only_visible_text():
    raw = (
        "<|channel|>analysis<|message|>secret chain of thought<|end|>"
        "<|start|>assistant<|channel|>final<|message|>Hello there.<|return|>"
    )
    visible, calls = harmony.split_harmony(raw)
    assert visible == "Hello there." and calls == []


def test_text_without_markers_is_untouched():
    assert harmony.split_harmony("plain reply") == ("plain reply", [])
    assert harmony.has_raw_markers("plain reply") is False


# --- through generate_stream -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_native_tool_call_with_empty_text_becomes_a_call_tag(monkeypatch):
    """Low-reasoning shape: no content at all, just tool_calls."""
    _stream(monkeypatch, [
        _sse({"choices": [{"delta": {"role": "assistant", "content": None}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "functions.get_time", "arguments": '{"zone":'}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ' "UTC"}'}}]}}]}),
        "data: [DONE]",
    ])
    out = "".join([c async for c in lsm.generate_stream("time?", tools=TOOLS)])
    assert out.strip().startswith("<call:get_time")
    assert "functions." not in out


@pytest.mark.asyncio
async def test_reasoning_content_is_never_yielded(monkeypatch):
    _stream(monkeypatch, [
        _sse({"choices": [{"delta": {"reasoning_content": "private thoughts"}}]}),
        _sse({"choices": [{"delta": {"content": "The answer is 4."}}]}),
        "data: [DONE]",
    ])
    out = "".join([c async for c in lsm.generate_stream("2+2?")])
    assert out == "The answer is 4."
    assert "private" not in out


@pytest.mark.asyncio
async def test_effort_is_medium_with_tools_and_low_for_chat(monkeypatch):
    _stream(monkeypatch, ["data: [DONE]"])
    [c async for c in lsm.generate_stream("hi", max_tokens=100, tools=TOOLS)]
    [c async for c in lsm.generate_stream("hi", max_tokens=100)]
    with_tools, chat = _Client.sent
    kagura = profiles.get("kagura")
    assert with_tools["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert chat["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert with_tools["max_tokens"] == 100 + kagura.token_budget("medium")
    assert chat["max_tokens"] == 100 + kagura.token_budget("low")


@pytest.mark.asyncio
async def test_raw_markers_in_the_text_stream_are_cleaned_after_the_turn(monkeypatch):
    _stream(monkeypatch, [
        _sse({"choices": [{"delta": {"content": "<|channel|>analysis<|message|>hidden<|end|>"}}]}),
        _sse({"choices": [{"delta": {"content":
            "<|start|>assistant<|channel|>final<|message|>Visible.<|return|>"}}]}),
        "data: [DONE]",
    ])
    out = "".join([c async for c in lsm.generate_stream("hi")])
    assert out == "Visible."


@pytest.mark.asyncio
async def test_a_profileless_model_gets_no_reasoning_fields(monkeypatch):
    monkeypatch.setenv("TALA_PROFILE", "legacy")
    _stream(monkeypatch, ["data: [DONE]"])
    [c async for c in lsm.generate_stream("hi", tools=TOOLS)]
    assert "chat_template_kwargs" not in _Client.sent[0]

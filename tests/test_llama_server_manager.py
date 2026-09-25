"""
generate_stream()'s SSE parsing -- content deltas and native tool_calls deltas.

Confirmed live: a tool-calling turn's streamed deltas carry ONLY
`tool_calls`, never `content` -- passing `tools=` and getting a real tool
call back produced a silently empty completion (`generate_stream` only ever
looked at `delta.get("content")`), which `_generate_reply` then reported as
"all providers failed" with an empty errors list, because no provider
actually raised -- it "succeeded" with nothing to say. Fixed by accumulating
the tool_calls deltas (OpenAI-style streaming splits one call across many
chunks: name arrives once, "arguments" arrives as incremental JSON-string
fragments) and converting them to the harness's own `<call:name .../>` tag
via the same `_tool_calls_to_call_tags` converter already used elsewhere,
rather than inventing a second tool-call text format.
"""
from __future__ import annotations

import json

import pytest

from python.inference import llama_server_manager as lsm


class _FakeStreamResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None):
        return _FakeStreamCtx(self._lines)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}"


@pytest.fixture(autouse=True)
def _fake_engine(monkeypatch):
    async def fake_ensure_running(preferred=None):
        class Handle:
            variant = "cpu"
            base_url = "http://127.0.0.1:0"
        return Handle()

    monkeypatch.setattr(lsm, "_ensure_running", fake_ensure_running)


def _patch_stream(monkeypatch, lines: list[str]) -> None:
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))


@pytest.mark.asyncio
async def test_plain_content_deltas_are_yielded(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"role": "assistant", "content": None}}]}),
        _sse({"choices": [{"delta": {"content": "Hello"}}]}),
        _sse({"choices": [{"delta": {"content": ", world"}}]}),
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)

    chunks = [c async for c in lsm.generate_stream("hi")]

    assert "".join(chunks) == "Hello, world"


@pytest.mark.asyncio
async def test_a_tool_call_split_across_chunks_becomes_a_call_tag(monkeypatch):
    """The exact shape confirmed against the real server: name arrives in
    one delta, "arguments" arrives incrementally in later ones."""
    lines = [
        _sse({"choices": [{"delta": {"role": "assistant", "content": None}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call1", "type": "function",
             "function": {"name": "get_system_info", "arguments": "{"}},
        ]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "}"}},
        ]}}]}),
        _sse({"choices": [{"finish_reason": "tool_calls", "delta": {}}]}),
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)

    chunks = [c async for c in lsm.generate_stream("hi", tools=[{"type": "function", "function": {"name": "get_system_info"}}])]
    result = "".join(chunks)

    assert result.strip().startswith("<call:get_system_info")
    assert result.strip().endswith("/>")


@pytest.mark.asyncio
async def test_no_tool_calls_means_no_call_tag(monkeypatch):
    """A caller that never passes tools sees no behaviour change at all."""
    lines = [
        _sse({"choices": [{"delta": {"content": "just text"}}]}),
        "data: [DONE]",
    ]
    _patch_stream(monkeypatch, lines)

    chunks = [c async for c in lsm.generate_stream("hi")]

    assert "".join(chunks) == "just text"
    assert "<call:" not in "".join(chunks)

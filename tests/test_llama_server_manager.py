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

import asyncio
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


# --- keeping the main model up: a server that is alive but not answering ---------------------------------------------------------------------


class _Proc:
    def __init__(self):
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.pid = 4242

    def terminate(self):
        self.terminated += 1
        self.returncode = 1

    def kill(self):
        self.killed += 1
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _handle():
    proc = _Proc()
    return lsm._ServerHandle(proc, 28200, lsm.EXTERNAL), proc


@pytest.mark.asyncio
async def test_a_server_that_stops_answering_is_stopped_so_the_supervisor_restarts_it(monkeypatch):
    handle, proc = _handle()
    monkeypatch.setattr(lsm, "_server", handle)
    monkeypatch.setattr(lsm, "_WATCHDOG_INTERVAL_S", 0.001)
    monkeypatch.setattr(lsm, "_WATCHDOG_FAILURES", 3)
    checks = []

    async def dead(base_url, timeout=5.0):
        checks.append(base_url)
        return False

    monkeypatch.setattr(lsm, "_health_ok", dead)
    await lsm._watchdog(handle)
    assert len(checks) == 3 and proc.terminated == 1


@pytest.mark.asyncio
async def test_one_slow_answer_is_not_a_failure(monkeypatch):
    handle, proc = _handle()
    monkeypatch.setattr(lsm, "_server", handle)
    monkeypatch.setattr(lsm, "_WATCHDOG_INTERVAL_S", 0.001)
    monkeypatch.setattr(lsm, "_WATCHDOG_FAILURES", 3)
    answers = iter([False, False, True, False, False, True, True])

    async def flaky(base_url, timeout=5.0):
        try:
            return next(answers)
        except StopIteration:
            handle.proc.returncode = 0  # end the loop: the server "exited" normally
            return True

    monkeypatch.setattr(lsm, "_health_ok", flaky)
    await lsm._watchdog(handle)
    assert proc.terminated == 0, "two failures in a row, twice, with a success between: never three in a row"


@pytest.mark.asyncio
async def test_the_watchdog_leaves_a_replaced_server_alone(monkeypatch):
    handle, proc = _handle()
    newer, _ = _handle()
    monkeypatch.setattr(lsm, "_server", newer)
    monkeypatch.setattr(lsm, "_WATCHDOG_INTERVAL_S", 0.001)

    async def dead(base_url, timeout=5.0):
        return False

    monkeypatch.setattr(lsm, "_health_ok", dead)
    await lsm._watchdog(handle)
    assert proc.terminated == 0


@pytest.mark.asyncio
async def test_a_server_that_will_not_stop_is_killed(monkeypatch):
    handle, proc = _handle()

    def stubborn():
        proc.terminated += 1  # terminate() has no effect: still running

    proc.terminate = stubborn
    monkeypatch.setattr(lsm, "_server", handle)
    monkeypatch.setattr(lsm, "_WATCHDOG_INTERVAL_S", 0.001)
    monkeypatch.setattr(lsm, "_WATCHDOG_FAILURES", 1)
    monkeypatch.setattr(lsm, "_TERMINATE_GRACE_S", 0.01)

    async def dead(base_url, timeout=5.0):
        return False

    async def never_exits():
        await asyncio.sleep(1)

    proc.wait = never_exits
    monkeypatch.setattr(lsm, "_health_ok", dead)
    await lsm._watchdog(handle)
    assert proc.killed == 1


@pytest.mark.asyncio
async def test_the_health_probe_reads_the_servers_own_health_endpoint(monkeypatch):
    import httpx

    seen = []

    class _Client:
        def __init__(self, *a, **k):
            seen.append(k.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await lsm._health_ok("http://127.0.0.1:1234", timeout=2.0) is True
    assert "http://127.0.0.1:1234/health" in seen

    class _Down(_Client):
        async def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    assert await lsm._health_ok("http://127.0.0.1:1234", timeout=2.0) is False


def test_starting_the_server_also_starts_its_watchdog():
    from pathlib import Path

    # read the file: an autouse fixture in this module replaces _ensure_running itself
    source = Path(lsm.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def _ensure_running"):][:1400]
    assert "_supervise(handle)" in body and "_watchdog(handle)" in body


def test_a_generation_records_the_engines_own_timings():
    import inspect

    source = inspect.getsource(lsm.generate_stream)
    assert "latency.record(" in source and "timings" in source


def test_a_server_that_exits_while_loading_fails_the_start_at_once():
    """A model llama-server cannot load makes it exit within a second; the start must not then poll a dead
    port for the whole startup timeout (60 s by default, and longer when TALA_LLAMA_STARTUP_TIMEOUT is set)."""
    import time as _time

    from python.inference import llama_server_manager as lsm

    class Dead:
        returncode = 1

    started = _time.perf_counter()
    assert asyncio.run(lsm._wait_healthy("http://127.0.0.1:1", timeout_s=30.0, proc=Dead())) is False
    assert _time.perf_counter() - started < 5.0


# --- when the server dies, its last words are kept -------------------------------------------------------------------------------------------
#
# 2026-10-24, the real app: a streaming reply from the main model was cut with `ReadError` eight seconds in and the request fell
# back to another model. llama-server's output was logged at debug level only, so what it said as it died was gone.


class _Stdout:
    def __init__(self, lines):
        self.lines = [ln.encode() + b"\n" for ln in lines]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


@pytest.mark.asyncio
async def test_the_servers_last_output_lines_are_kept():
    handle, proc = _handle()
    proc.stdout = _Stdout([f"line {i}" for i in range(100)])
    await lsm._pump_output(proc, lsm.EXTERNAL, handle.tail)
    assert list(handle.tail)[0] == "line 60" and list(handle.tail)[-1] == "line 99"
    assert len(handle.tail) == lsm._OUTPUT_TAIL_LINES


@pytest.mark.asyncio
async def test_an_unexpected_exit_is_logged_with_what_the_server_said(monkeypatch, caplog):
    import logging

    handle, proc = _handle()
    proc.returncode = 3
    handle.tail.extend(["load_tensors: offloaded 24/25 layers", "CUDA error: out of memory", "ggml_abort"])
    monkeypatch.setattr(lsm, "_server", handle)
    monkeypatch.setattr(lsm, "_RESTART_BASE_DELAY_S", 0.0)
    started = []

    async def fake_ensure(preferred=None):
        started.append(preferred)

    monkeypatch.setattr(lsm, "_ensure_running", fake_ensure)
    with caplog.at_level(logging.ERROR, logger=lsm.logger.name):
        await lsm._supervise(handle)
    assert "exited unexpectedly" in caplog.text and "code 3" in caplog.text
    assert "CUDA error: out of memory" in caplog.text and "ggml_abort" in caplog.text
    assert started, "it is still restarted, as before"


@pytest.mark.asyncio
async def test_a_deliberate_stop_is_not_reported_as_a_crash(monkeypatch, caplog):
    import logging

    handle, proc = _handle()
    proc.returncode = 1
    monkeypatch.setattr(lsm, "_server", None)  # stop_server clears this first, which is what marks a stop as intended
    with caplog.at_level(logging.ERROR, logger=lsm.logger.name):
        await lsm._supervise(handle)
    assert "exited unexpectedly" not in caplog.text

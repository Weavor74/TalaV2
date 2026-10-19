"""
Session provider override, visible fallback, Gemini thought_signature, and
RPC status while a slow inference runs.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_roundtrip = _load("openai_tool_roundtrip", "python/harness/openai_tool_roundtrip.py")
_selection = _load("provider_selection", "python/harness/provider_selection.py")
_occupancy = _load("rpc_occupancy", "python/harness/rpc_occupancy.py")

accumulate_streamed_tool_calls = _roundtrip.accumulate_streamed_tool_calls
is_thought_signature_error = _roundtrip.is_thought_signature_error
native_tool_followup_messages = _roundtrip.native_tool_followup_messages
normalize_tool_calls = _roundtrip.normalize_tool_calls
format_fallback_notice = _selection.format_fallback_notice
remote_provider_ready = _selection.remote_provider_ready
resolve_provider_chain = _selection.resolve_provider_chain
skip_provider_attempt = _selection.skip_provider_attempt
RpcOccupancy = _occupancy.RpcOccupancy
_run_blocking = _occupancy._run_blocking
run_rpc_handler = _occupancy.run_rpc_handler


def _gemini(**overrides):
    row = {
        "id": "gemini-1",
        "type": "remote",
        "name": "Gemini",
        "priority": 2,
        "status": "online",
        "config": {
            "service": "gemini",
            "model": "gemini-3.8-flash",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
            "apiKey": "test-key-not-a-secret",
        },
        "locked": False,
    }
    row.update(overrides)
    if "config" in overrides:
        cfg = dict(row["config"])
        cfg.update(overrides["config"])
        row["config"] = cfg
    return row


def _embedded():
    return {
        "id": "embedded-builtin",
        "type": "embedded",
        "name": "Tala Embedded Engine",
        "priority": 999,
        "status": "online",
        "config": {"model": "cpu"},
        "locked": True,
    }


def _ollama_offline():
    return {
        "id": "ollama-local",
        "type": "local",
        "name": "ollama local",
        "priority": 4,
        "status": "offline",
        "config": {"engine": "ollama", "endpoint": "http://localhost:11434/v1"},
        "locked": False,
    }


def test_session_override_is_first_on_every_turn():
    llama = {
        "id": "llama-cpp",
        "type": "local",
        "name": "llama.cpp",
        "priority": 1,
        "status": "online",
        "config": {},
        "locked": False,
    }
    route = resolve_provider_chain(
        [llama, _gemini(), _embedded(), _ollama_offline()],
        request_provider_id="gemini-1",
    )
    assert route.providers[0]["id"] == "gemini-1"
    assert route.request_provider["id"] == "gemini-1"
    assert skip_provider_attempt(route.providers[0], route.forced_provider, route.request_provider) is False


def test_unknown_session_id_does_not_silently_become_embedded_first():
    route = resolve_provider_chain(
        [_embedded(), _gemini()],
        request_provider_id="missing-id",
    )
    assert route.request_provider is None
    assert route.providers[0]["type"] == "remote"


def test_preferred_failure_notice_names_who_took_over():
    notice = format_fallback_notice(
        _gemini(),
        _embedded(),
        "HTTP Error 400: Function call is missing a thought_signature",
    )
    assert "Gemini" in notice
    assert "Tala Embedded Engine" in notice
    assert "thought_signature" in notice
    assert "Fell back" in notice


def test_gemini_requires_key_and_gets_default_url():
    bare = _gemini()
    bare["config"] = {"service": "gemini", "model": "gemini-3.8-flash", "apiKey": "k"}
    ok, _ = remote_provider_ready(bare)
    assert ok is True
    assert "generativelanguage.googleapis.com" in bare["config"]["endpoint"]

    no_key = _gemini()
    no_key["config"] = {
        "service": "gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "apiKey": "",
    }
    ok, reason = remote_provider_ready(no_key)
    assert ok is False
    assert "API key" in reason
    assert "sk-" not in reason
    assert "test-key" not in reason


def test_thought_signature_is_round_tripped_on_assistant_tool_calls():
    incoming = [{
        "id": "call_web",
        "type": "function",
        "function": {"name": "web_search", "arguments": '{"query":"news"}'},
        "extra_content": {"google": {"thought_signature": "SIG-abc"}},
    }]
    follow = native_tool_followup_messages(
        "", incoming, "web_search", [{"title": "ok"}],
    )
    assistant, tool = follow
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG-abc"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "call_web"
    assert "news" not in str(assistant["tool_calls"][0]["extra_content"])


def test_streamed_tool_call_keeps_gemini_extra_content():
    acc: dict[int, dict] = {}
    accumulate_streamed_tool_calls([{
        "index": 0,
        "id": "c1",
        "function": {"name": "web_search", "arguments": ""},
        "extra_content": {"google": {"thought_signature": "STREAM-SIG"}},
    }], acc)
    accumulate_streamed_tool_calls([{
        "index": 0,
        "function": {"arguments": '{"query":"x"}'},
    }], acc)
    calls = normalize_tool_calls([acc[0]])
    assert calls[0]["extra_content"]["google"]["thought_signature"] == "STREAM-SIG"


def test_thought_signature_400_is_not_a_tools_rejection():
    err = "HTTP Error 400: Function call FC1 is missing a thought_signature"
    assert is_thought_signature_error(err) is True
    assert is_thought_signature_error("does not support tools") is False


@pytest.mark.asyncio
async def test_rpc_status_answers_while_slow_inference_runs():
    occupancy = RpcOccupancy()

    async def slow_inference():
        occupancy._enter_long("inference.generate")
        try:
            await _run_blocking(time.sleep, 0.4)
        finally:
            occupancy._exit_long("inference.generate")
        return "done"

    async def status():
        await asyncio.sleep(0)
        return occupancy.snapshot()

    infer_task = asyncio.create_task(
        run_rpc_handler("inference.generate", slow_inference)
    )
    await asyncio.sleep(0.05)
    snap = await asyncio.wait_for(
        run_rpc_handler("gateway.status", status),
        timeout=0.2,
    )
    assert snap["busy"] is True
    assert snap["busy_method"] == "inference.generate"
    assert await infer_task == "done"
    assert occupancy.busy is False


def test_server_wires_override_roundtrip_and_occupancy():
    server = (_ROOT / "python/harness/server.py").read_text(encoding="utf-8")
    assert "resolve_provider_chain" in server
    assert "native_tool_followup_messages" in server
    assert "is_thought_signature_error" in server
    assert "run_rpc_handler" in server
    assert "fallback_notice" in server


@pytest.mark.asyncio
async def test_run_rpc_handler_marks_generate_busy():
    seen = {}

    async def generate():
        seen["busy"] = True
        assert True
        await asyncio.sleep(0.01)
        return "ok"

    result = await run_rpc_handler("inference.generate", generate)
    assert result == "ok"
    assert seen["busy"] is True

"""
_generate_reply's local/remote provider branch must not drop a native tool
call the way the embedded engine's generate_stream() used to (fixed earlier
this session in llama_server_manager.py). That fix only covered the embedded
path -- this is the same bug in _generate_reply's own non-streaming HTTP
branch, found while live-verifying send_to_agent in a real room: a room
agent's turn happened to be served by the embedded provider (priority 1) and
answered a general-knowledge question directly rather than delegating, which
is a model-compliance question, not a wiring one -- but reading the code
alongside it turned up this second, previously-unfixed instance of the exact
same class of bug for local/remote providers.

A non-streaming OpenAI-shaped response for a tool call carries `tool_calls`
on the message and no `content` at all; the old code only ever read
`.content`, silently discarding the call.
"""
from __future__ import annotations

import json

import pytest


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return self._response


def _local_provider_row():
    return {
        "id": "ollama-test", "type": "local", "name": "OLLAMA - test-model",
        "priority": 1, "status": "online", "locked": False,
        "config": {"engine": "ollama", "model": "test-model"},
    }


TOOLS = [{"type": "function", "function": {"name": "get_system_info", "description": "", "parameters": {}}}]


@pytest.mark.asyncio
async def test_a_local_providers_tool_call_is_not_dropped(monkeypatch):
    import httpx
    from python.harness import server

    async def fake_load():
        return [_local_provider_row()]

    response = _FakeResponse({
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call1", "type": "function",
                "function": {"name": "get_system_info", "arguments": "{}"},
            }],
        }}]
    })

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(response))

    async def fake_tool_round(reply_text, tools, principal):
        assert reply_text.strip().startswith("<call:get_system_info")
        return None  # stop after confirming the tag arrived intact

    monkeypatch.setattr("sdk.agent.agent_reply_tools.run_agent_tool_round", fake_tool_round)

    result = await server._generate_reply(
        [{"role": "user", "content": "what's my system info?"}],
        tools=TOOLS,
        principal=object(),
    )

    assert result.strip().startswith("<call:get_system_info")


@pytest.mark.asyncio
async def test_a_local_providers_plain_content_is_unaffected(monkeypatch):
    """Regression guard: the fix must not touch the ordinary, no-tool-call path."""
    import httpx
    from python.harness import server

    async def fake_load():
        return [_local_provider_row()]

    response = _FakeResponse({
        "choices": [{"message": {"role": "assistant", "content": "Hello there."}}]
    })

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(response))

    result = await server._generate_reply([{"role": "user", "content": "hi"}])

    assert result == "Hello there."


# -- tool_choice ("required" forcing, from an agent's config) ---------------

class _CapturingAsyncClient:
    """Records the payload a local/remote POST actually sent."""

    def __init__(self, response: _FakeResponse, captured: dict):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self._captured["payload"] = json
        return self._response


@pytest.mark.asyncio
async def test_tool_choice_reaches_a_local_providers_payload(monkeypatch):
    import httpx
    from python.harness import server

    async def fake_load():
        return [_local_provider_row()]

    response = _FakeResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    captured: dict = {}

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _CapturingAsyncClient(response, captured))

    await server._generate_reply(
        [{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="required",
    )

    assert captured["payload"]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_no_tool_choice_means_no_key_in_the_payload(monkeypatch):
    """Every existing caller that never set config.tool_choice must see the
    exact same request shape as before this feature existed."""
    import httpx
    from python.harness import server

    async def fake_load():
        return [_local_provider_row()]

    response = _FakeResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    captured: dict = {}

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _CapturingAsyncClient(response, captured))

    await server._generate_reply([{"role": "user", "content": "hi"}], tools=TOOLS)

    assert "tool_choice" not in captured["payload"]


@pytest.mark.asyncio
async def test_the_embedded_provider_never_receives_tool_choice(monkeypatch):
    """Confirmed live: "required" derails the embedded llama.cpp fork into a
    repetition loop instead of forcing a tool call -- it must never be
    forwarded there, regardless of what an agent's config asks for."""
    from python.harness import server

    async def fake_load():
        return [{
            "id": "embedded-builtin", "type": "embedded", "name": "Tala Embedded Engine",
            "priority": 1, "status": "online", "locked": True, "config": {},
        }]

    captured = {}

    async def fake_check_model_cached():
        return True

    async def fake_generate_stream(*args, **kwargs):
        captured["kwargs"] = kwargs
        yield "ok"

    import python.inference.llama_server_manager as lsm
    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(lsm, "check_model_cached", lambda: True)
    monkeypatch.setattr(lsm, "generate_stream", fake_generate_stream)

    await server._generate_reply(
        [{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="required",
    )

    assert "tool_choice" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_tool_choice_only_forces_the_first_pass(monkeypatch):
    """Once a tool has actually been called, later rounds must not keep
    forcing another one -- the model needs to be free to just answer."""
    import httpx
    from python.harness import server

    async def fake_load():
        return [_local_provider_row()]

    call_payloads = []

    class _SequencedClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            call_payloads.append(json)
            if len(call_payloads) == 1:
                return _FakeResponse({"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"function": {"name": "get_system_info", "arguments": "{}"}}],
                }}]})
            return _FakeResponse({"choices": [{"message": {"role": "assistant", "content": "final answer"}}]})

    monkeypatch.setattr(server, "load_inference_providers", fake_load)
    monkeypatch.setattr(httpx, "AsyncClient", _SequencedClient)

    async def fake_tool_round(reply_text, tools, principal):
        if len(call_payloads) > 1:
            return None
        return "<response: get_system_info result={} />"

    monkeypatch.setattr("sdk.agent.agent_reply_tools.run_agent_tool_round", fake_tool_round)

    result = await server._generate_reply(
        [{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="required",
    )

    assert result == "final answer"
    assert len(call_payloads) == 2
    assert call_payloads[0]["tool_choice"] == "required"
    assert "tool_choice" not in call_payloads[1]


@pytest.mark.asyncio
async def test_resolve_agent_tools_reads_a_valid_tool_choice(monkeypatch):
    """resolve_agent_tools is exercised end-to-end elsewhere (test_agent_reply_tools.py);
    this covers only the tool_choice validation this feature added."""
    from python.harness import db
    from sdk.agent.agent_reply_tools import resolve_agent_tools

    async def fake_fetchrow(*a, **k):
        return {"config": {"tool_allowlist": ["get_system_info"], "tool_choice": "required"}}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(ToolRegistry, "get_tools", lambda: [
        {"name": "get_system_info", "description": "d", "schema": {"type": "object", "properties": {}, "required": []}},
    ])

    _, _, tool_choice = await resolve_agent_tools("a1")
    assert tool_choice == "required"


@pytest.mark.asyncio
async def test_resolve_agent_tools_ignores_an_unrecognised_tool_choice(monkeypatch):
    from python.harness import db
    from sdk.agent.agent_reply_tools import resolve_agent_tools

    async def fake_fetchrow(*a, **k):
        return {"config": {"tool_allowlist": ["get_system_info"], "tool_choice": "force-please"}}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(ToolRegistry, "get_tools", lambda: [
        {"name": "get_system_info", "description": "d", "schema": {"type": "object", "properties": {}, "required": []}},
    ])

    _, _, tool_choice = await resolve_agent_tools("a1")
    assert tool_choice is None

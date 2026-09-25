"""
make_structured_decision -- Jev mode exposed as a tool call, not a hidden
pipeline stage.

Built and verified end-to-end in the engine-swap work
(llama_server_manager.decide()), but nothing ever called it. This registers
it the same way vault_tools.py registers "things the local LLM could
reference": an ordinary tool the agent chooses to invoke.
"""
import pytest

from sdk.agent import decision_tools  # noqa: F401  (registers the tool)
from sdk.agent.decision_tools import make_structured_decision
from sdk.agent.tools import ToolRegistry

SCHEMA = {"sentiment": {"type": "enum", "choices": ["positive", "negative"], "description": "overall sentiment"}}


def test_the_tool_is_registered():
    """Registered, not merely defined -- an unregistered handler is this
    codebase's most repeated defect."""
    names = {t["name"] for t in ToolRegistry.get_tools()}
    assert "make_structured_decision" in names


@pytest.mark.asyncio
async def test_refuses_cleanly_when_the_engine_is_not_running(monkeypatch):
    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: None)
    called = False

    async def _decide(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await make_structured_decision(schema=SCHEMA, contexts=["I love this!"])

    assert result["success"] is False
    assert "not running" in result["error"]
    assert called is False, "decide() must not be called when the engine is down"


@pytest.mark.asyncio
async def test_returns_the_decision_when_the_engine_is_running(monkeypatch):
    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    captured = {}

    async def _decide(schema, contexts, instructions=""):
        captured["schema"] = schema
        captured["contexts"] = contexts
        captured["instructions"] = instructions
        return {"results": [{"decision": {"sentiment": "positive"}}]}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await make_structured_decision(
        schema=SCHEMA, contexts=["I love this!"], instructions="Classify sentiment.",
    )

    assert result["success"] is True
    assert result["results"][0]["decision"] == {"sentiment": "positive"}
    assert captured["schema"] == SCHEMA
    assert captured["contexts"] == ["I love this!"]
    assert captured["instructions"] == "Classify sentiment."


@pytest.mark.asyncio
async def test_a_decide_failure_is_reported_not_raised(monkeypatch):
    """A tool call must never crash the turn -- the caller sees a refusal,
    not an exception."""
    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    async def _decide(*a, **k):
        raise RuntimeError("server unreachable")

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await make_structured_decision(schema=SCHEMA, contexts=["I love this!"])

    assert result["success"] is False
    assert "server unreachable" in result["error"]

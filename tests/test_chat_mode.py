"""
determine_effective_mode() -- per-turn chat mode via Jev mode, ephemeral.

Chat mode used to be purely manual: three UI buttons, zero content-based
detection, and switching mode flips which persona/agent is active in the DB
and clears history. determine_effective_mode() adds a per-turn override
layered on top of that -- it shapes only this turn's prompt assembly and
never writes chat_mode.json or touches which persona is active, so a single
ambiguous message cannot flip who Tala is for the rest of the conversation.

The schema is two independent booleans (in_character, wants_technical_help),
not a single three-way enum -- measured directly: the enum locked onto one
label regardless of message content on both the 0.5B and 1.5B models tried,
while the decomposed booleans actually discriminated on real test input.
"""
import pytest

from python.harness import chat_mode


@pytest.fixture(autouse=True)
def _isolated_mode_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_mode, "_get_chat_mode_file_path", lambda: tmp_path / "chat_mode.json")


@pytest.mark.asyncio
async def test_falls_back_to_persisted_mode_when_the_engine_is_not_running(monkeypatch):
    """No cold start is ever paid for this optional enhancement: decide()
    must not even be called when the engine is down."""
    chat_mode.save_current_chat_mode("assistant")

    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: None)
    called = False

    async def _decide(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await chat_mode.determine_effective_mode("Let's get to work on the bug.")

    assert result == "assistant"
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("in_character,wants_technical_help,expected", [
    (True, False, "rp"),
    (False, True, "assistant"),
    (True, True, "hybrid"),
])
async def test_each_boolean_combination_derives_the_right_mode(monkeypatch, in_character, wants_technical_help, expected):
    chat_mode.save_current_chat_mode("assistant" if expected != "assistant" else "rp")

    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    async def _decide(schema, contexts, instructions=""):
        assert set(schema) == {"in_character", "wants_technical_help"}
        assert contexts == ["*leans in, voice low* tell me about your day"]
        return {"results": [{"decision": {
            "in_character": in_character, "wants_technical_help": wants_technical_help,
        }}]}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await chat_mode.determine_effective_mode("*leans in, voice low* tell me about your day")

    assert result == expected


@pytest.mark.asyncio
async def test_neither_signal_firing_defers_to_the_persisted_mode(monkeypatch):
    """Not confident either way -- guessing would be worse than deferring."""
    chat_mode.save_current_chat_mode("hybrid")

    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    async def _decide(*a, **k):
        return {"results": [{"decision": {"in_character": False, "wants_technical_help": False}}]}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await chat_mode.determine_effective_mode("hi")

    assert result == "hybrid"


@pytest.mark.asyncio
async def test_the_override_is_ephemeral_not_persisted(monkeypatch):
    """The whole point: this never calls save_current_chat_mode()."""
    chat_mode.save_current_chat_mode("assistant")

    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    async def _decide(*a, **k):
        return {"results": [{"decision": {"in_character": True, "wants_technical_help": False}}]}

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await chat_mode.determine_effective_mode("in character now")

    assert result == "rp"
    assert chat_mode.get_current_chat_mode() == "assistant", (
        "determine_effective_mode() must never write chat_mode.json"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "malformed_response"])
async def test_falls_back_to_persisted_mode_on_any_failure(monkeypatch, failure):
    """A classification hiccup must not cost the user their turn."""
    chat_mode.save_current_chat_mode("hybrid")

    from python.inference import llama_server_manager as lsm

    monkeypatch.setattr(lsm, "active_variant", lambda: "cpu")

    async def _decide(*a, **k):
        if failure == "raises":
            raise RuntimeError("server unreachable")
        return {"results": []}  # malformed_response: no decision to index

    monkeypatch.setattr(lsm, "decide", _decide)

    result = await chat_mode.determine_effective_mode("anything")

    assert result == "hybrid"

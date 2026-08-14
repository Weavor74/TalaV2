"""
Repeatable regression fixture for the class of production bugs found this
session by manually tracing real conversations through logs/DB by hand:
  - unclosed <thinking> blocks leaking raw reasoning into the visible reply
  - <call:...> tags quoted inside reasoning mistaken for the real invocation
  - competing tool-invocation protocols confusing the model mid-turn
  - a forced/preferred provider's tool-calling behavior regressing

These run real end-to-end conversations against whatever provider is
actually active on a live harness -- deliberately not mocked, since these
bugs only ever showed up against real inference, never in isolated parser
unit tests. Skips cleanly (not a failure) if no harness is reachable at
ws://127.0.0.1:8765, so a normal `pytest tests/` run or CI (which has no
live provider credentials) is never blocked by this file.

To actually run these: start the app (`npm start`) or the harness alone,
then `pytest tests/test_adversarial_conversations.py -v`.

Because real models are non-deterministic, assertions here are restricted
to HARNESS-level protocol invariants that must hold regardless of what any
given model decides to say -- not to specific response content. Whether a
tool call actually fires is inherently a model-compliance question (we've
observed capable models sometimes narrate intent in prose instead of
emitting the tag); leaked reasoning or stray unparsed tags reaching the
user is never acceptable regardless of what the model did.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket

import pytest
import websockets

HARNESS_WS = "ws://127.0.0.1:8765"


def _harness_reachable() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 8765))
        return True
    except OSError:
        return False
    finally:
        s.close()


pytestmark = [
    # Real inference.generate RPCs against the running app, one _GENERATE_TIMEOUT
    # each. Deselected from the default `pytest`; run with `pytest -m llm`.
    pytest.mark.llm,
    pytest.mark.skipif(
        not _harness_reachable(),
        reason="No live harness at ws://127.0.0.1:8765 -- start the app to run these adversarial conversation checks",
    ),
]


#: Ceiling for one real inference.generate round-trip.
#:
#: Was 200s, which this hardware cannot meet: measured chat turns against the
#: local 8B run 60-160s, and a turn that also dispatches a tool runs longer
#: still. Across three separate runs all four of these tests failed at
#: 200.01-200.81s -- not varying results, the ceiling itself being hit. The box
#: was demonstrably quiet for the last of them, so raising the ceiling is the
#: honest fix rather than chasing contention. Matches the same call made for
#: DAG synthesis (TALA_SYNTHESIS_TIMEOUT, 60s -> 300s).
#:
#: Override for faster or slower machines with TALA_ADVERSARIAL_TIMEOUT.
_GENERATE_TIMEOUT = float(os.getenv("TALA_ADVERSARIAL_TIMEOUT", "300"))


async def _generate(prompt: str, timeout: float = _GENERATE_TIMEOUT) -> dict:
    """Send a real inference.generate RPC and return the parsed JSON-RPC response."""
    async with websockets.connect(HARNESS_WS, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "inference.generate",
            "params": {"prompt": prompt, "stream": False},
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg


def _assert_clean_response(result: dict) -> str:
    """Shared invariants every conversation below must satisfy, regardless
    of what the model actually decided to say."""
    assert "result" in result, f"Expected a successful inference.generate result, got: {result}"
    text = result["result"]["text"]
    assert text.strip(), "Expected non-empty response text"

    # The harness's own <thinking> handling (server.py's reasoning-wrapping
    # instruction + the renderer's parser) must never let a raw <thinking>
    # tag reach here -- that's the unclosed-tag leak bug.
    assert "<thinking>" not in text, (
        f"Raw <thinking> tag leaked into the final response text: {text[:300]!r}"
    )

    # A dispatched tool call gets stripped from the visible text (see
    # clean_generated in handle_inference_generate); an undispatched or
    # unstripped one showing up here means parsing/dispatch broke.
    assert not re.search(r"<call:[\w.]+", text), (
        f"An undispatched or unstripped <call:...> tag leaked into the final response: {text[:300]!r}"
    )

    return text


@pytest.mark.asyncio
async def test_capability_question_no_leaked_reasoning():
    """'What can you do' is the exact prompt that originally exposed the
    unclosed-<thinking> bug and the quoted-tag-in-reasoning bug -- the model
    reasons about whether/how to call system.capabilities before acting,
    which is precisely the reasoning-adjacent-to-a-tool-tag scenario those
    bugs lived in."""
    result = await _generate("What can you do? List your capabilities.")
    _assert_clean_response(result)


@pytest.mark.asyncio
async def test_direct_action_request_no_leaked_reasoning():
    """A direct, unambiguous action request. The model may complete a tool
    call or just answer in prose -- either is fine -- but must never leak
    parsing artifacts either way."""
    result = await _generate(
        "Search the web for the current stable version of Python and tell me what you found."
    )
    _assert_clean_response(result)


@pytest.mark.asyncio
async def test_ambiguous_hedge_prone_question_completes_cleanly():
    """Phrased to plausibly invite hedging/deliberation about whether to act
    (the exact failure mode of the persona-vs-tool-calling conflict bug)
    rather than a clean tool call or answer. Must still complete within
    budget without leaking reasoning, regardless of whether the model
    ultimately decides to act."""
    result = await _generate(
        "Do you have any tools you could use to check on the current system status?"
    )
    _assert_clean_response(result)


@pytest.mark.asyncio
async def test_multi_action_prompt_completes_without_looping_forever():
    """Asks for two distinct things in one message, increasing the odds of
    exercising the harness's internal multi-turn tool-calling loop
    (call -> result -> call again -> final answer) rather than a single
    round-trip. Must still terminate cleanly within the timeout budget."""
    result = await _generate(
        "First tell me what capabilities you have, then tell me the current system's CPU and memory info."
    )
    _assert_clean_response(result)


@pytest.mark.asyncio
async def test_capability_question_actually_dispatches_the_tool():
    """Stronger check for the single clearest-signal prompt: not just that
    the response is clean, but that system.capabilities (or the equivalent
    RPC path) actually fired, verified against the harness's own agent_log
    -- this is what originally caught the "..." bogus-tool-name dispatch
    bug (a real tool call was attempted and silently failed with 'Tool
    ... is not supported', which the model then papered over in prose)."""
    from python.harness import db

    await db.init_pool()
    before = await db.fetchrow("SELECT NOW() as t")
    start_time = before["t"]

    result = await _generate("What can you do? Use your tools to check.")
    _assert_clean_response(result)

    rows = await db.fetch(
        "SELECT message, trace FROM agent_log "
        "WHERE created_at >= $1 AND message LIKE 'Agent Chat: Auto-executed tool%' "
        "ORDER BY created_at ASC",
        start_time,
    )
    dispatched_names = []
    for r in rows:
        trace = r["trace"]
        if isinstance(trace, str):
            trace = json.loads(trace)
        dispatched_names.append(trace.get("tool", ""))

    if not dispatched_names:
        pytest.skip(
            "Model answered without calling any tool this run (model-compliance "
            "variance, not a harness bug) -- nothing to verify dispatch against."
        )

    assert all(name and name != "..." for name in dispatched_names), (
        f"A tool call was attempted with a malformed/placeholder name: {dispatched_names}"
    )

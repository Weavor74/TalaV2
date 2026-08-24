"""
The renderer must be able to answer, and the task board must not execute what
it renders.

``browser.get_dom`` and ``browser.execute_js`` were stubs that returned
``{"status": "broadcast_sent"}`` -- one broadcast nothing at all, the other
broadcast at a channel with no listener -- and both reported success (UI-014,
UI-015). The missing piece was a return path: broadcasts are fire-and-forget,
so even a correct listener had nowhere to put its answer.

Separately, task descriptions went into ``innerHTML`` unescaped (UI-029). A
goal can be seeded from web research, so that is untrusted text.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from python.harness import ui_bridge

RENDERER = Path("renderer/renderer.js")


# -- UI-014 / UI-015: the bridge ---------------------------------------------

@pytest.fixture
def reaching(monkeypatch):
    """Install a fake broadcast and report what it was given."""
    import python.harness.server as server
    sent = {}

    def install(clients, responder=None):
        async def _broadcast(method, params=None):
            sent["method"] = method
            sent["params"] = dict(params or {})
            if responder and clients:
                asyncio.get_running_loop().call_soon(
                    responder, params["request_id"]
                )
            return clients
        monkeypatch.setattr(server, "broadcast_notification", _broadcast)
        return sent

    return install


@pytest.mark.asyncio
async def test_no_connected_client_is_an_error_not_a_success(reaching):
    reaching(clients=0)
    with pytest.raises(ui_bridge.NoListener):
        await ui_bridge.request("browser.get_dom", {})


@pytest.mark.asyncio
async def test_a_result_comes_back_to_the_caller(reaching):
    sent = reaching(
        clients=1,
        responder=lambda rid: ui_bridge.resolve(rid, {"html": "<p>hi</p>"}),
    )
    result = await ui_bridge.request("browser.get_dom", {"selector": "main"})

    assert result == {"html": "<p>hi</p>"}
    assert sent["params"]["selector"] == "main"
    assert "request_id" in sent["params"], "the renderer cannot answer without a correlation id"


@pytest.mark.asyncio
async def test_a_renderer_side_failure_arrives_as_a_failure(reaching):
    reaching(
        clients=1,
        responder=lambda rid: ui_bridge.resolve(rid, None, "ReferenceError: x is not defined"),
    )
    with pytest.raises(ui_bridge.BridgeError, match="ReferenceError"):
        await ui_bridge.request("browser.execute_js", {"code": "x"})


@pytest.mark.asyncio
async def test_a_listener_that_never_answers_times_out(reaching):
    reaching(clients=1)  # delivered, but nothing responds
    with pytest.raises(asyncio.TimeoutError):
        await ui_bridge.request("browser.get_dom", {}, timeout=0.2)


@pytest.mark.asyncio
async def test_no_future_is_leaked_by_a_timeout(reaching):
    reaching(clients=1)
    with pytest.raises(asyncio.TimeoutError):
        await ui_bridge.request("browser.get_dom", {}, timeout=0.1)
    assert ui_bridge.pending_count() == 0


def test_a_late_answer_is_dropped_rather_than_raising():
    """By the time a late answer arrives the caller has been told the truth."""
    assert ui_bridge.resolve("no-such-request", {"a": 1}) is False


def test_the_renderer_has_handlers_for_both_channels():
    """The listeners are the half that was missing; without them this is a void."""
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "'browser.get_dom'" in renderer
    assert "'browser.execute_js'" in renderer
    assert "ui.bridge_result" in renderer, "the renderer has no way to answer"


def test_the_renderer_answers_on_failure_too():
    """A handler that only answers on success recreates the silent-success bug."""
    renderer = RENDERER.read_text(encoding="utf-8")
    block = renderer.split("async handleAgentExecuteJs(", 1)[1].split("\n  },", 1)[0]
    assert "catch" in block
    assert block.count("_answerBridge") >= 3, "not every exit path answers"


# -- UI-029: the task card ----------------------------------------------------

def test_the_task_card_escapes_every_untrusted_field():
    renderer = RENDERER.read_text(encoding="utf-8")
    block = renderer.split("const rawDesc =", 1)[1].split("card.querySelector", 1)[0]

    for field in ("shortDesc", "workerName"):
        assert f"${{{field}}}" in block, f"{field} is not interpolated into the card"

    assert "escapeHtml(rawDesc" in renderer
    assert "escapeHtml(t.assigned_agent_id" in renderer or "escapeHtml(String(t.assigned_agent_id" in renderer

    # nothing raw from the task record may reach innerHTML
    raw = re.findall(r"\$\{t\.(\w+)\}", block)
    assert not raw, f"unescaped task fields still interpolated: {raw}"


def test_a_null_description_does_not_break_the_board():
    """t.description.length on a null threw inside forEach; the outer catch
    swallowed it and the whole board rendered empty."""
    renderer = RENDERER.read_text(encoding="utf-8")
    assert "typeof t.description === 'string'" in renderer

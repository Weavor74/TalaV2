"""
The renderer's thumbs up / thumbs down / "that's wrong" controls, the settings toggle, the debug status, and the
admin reset reach node learning through RPCs in python/harness/learning_rpc.py.

Rules these tests pin:

* feedback moves the confidence of the nodes behind a reply, and nothing else;
* every parameter is validated (a bounded list of plain node ids, a known kind) before anything is touched;
* learning disabled means feedback is acknowledged but changes nothing, and says so;
* the emergency halt refuses every learning.* method (the halt gate's allow-list is default-deny);
* "reset weights" is an admin action: it needs an explicit confirm, and it runs through the tool registry, so the
  AuthorizationGate, the leases and the risk classifier all apply.
"""
from __future__ import annotations

import json

import pytest

from python.harness import halt_gate, learning_rpc
from sdk.agent import node_learning as nl
from sdk.agent.parser import parse_file

METHODS = ("learning.feedback", "learning.status", "learning.set_enabled", "learning.dream_now", "learning.reset_weights")


def make_node(directory, node_id, relevance="0.80", confidence="0.80"):
    path = directory / f"{node_id}.html"
    path.write_text(f'''<!DOCTYPE html>
<html><head><meta name="tala-version" content="neural-node/v2"></head><body>
<article
    data-node-id="{node_id}"
    data-node-type="concept"
    data-execution-authority="0.50"
    data-schema-version="neural-node/v2"
    data-weight-relevance="{relevance}"
    data-weight-confidence="{confidence}"
    data-weight-governance-importance="0.50">
    <section id="semantic-memory">Apples are a delicious red fruit that grow on trees.</section>
    <script type="application/json" id="node-state">{{"title": "{node_id}"}}</script>
</article>
</body></html>
''', encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    (tmp_path / "nodes").mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setattr(nl, "_halted", lambda: False)
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: ("h", "id", "anchored", "", None))
    return tmp_path


async def call(handler, params):
    return json.loads(await handler(params, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("up", 0.85), ("down", 0.70), ("wrong", 0.60)])
async def test_feedback_moves_the_confidence_of_the_named_nodes(userdata, kind, expected):
    path = make_node(userdata / "nodes", "apples")
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.feedback"], {"node_ids": ["apples"], "kind": kind})
    assert reply["result"]["changes"][0]["field"] == "weight_confidence"
    assert parse_file(str(path)).weight_confidence == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {},
    {"node_ids": [], "kind": "up"},
    {"node_ids": "apples", "kind": "up"},
    {"node_ids": ["apples"], "kind": "love-it"},
    {"node_ids": ["apples"]},
    {"node_ids": [f"n{i}" for i in range(9)], "kind": "up"},
    {"node_ids": ["../../etc/passwd"], "kind": "up"},
    {"node_ids": [7], "kind": "up"},
])
async def test_bad_feedback_is_refused_before_anything_is_touched(userdata, params):
    path = make_node(userdata / "nodes", "apples")
    before = path.read_bytes()
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.feedback"], params)
    assert "error" in reply
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_feedback_on_an_unknown_node_changes_nothing_and_is_not_an_error(userdata):
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.feedback"], {"node_ids": ["no-such-node"], "kind": "down"})
    assert reply["result"]["changes"] == []


@pytest.mark.asyncio
async def test_feedback_while_learning_is_off_is_acknowledged_and_changes_nothing(userdata, monkeypatch):
    path = make_node(userdata / "nodes", "apples")
    before = path.read_bytes()
    monkeypatch.setenv("TALA_LEARNING", "0")
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.feedback"], {"node_ids": ["apples"], "kind": "wrong"})
    assert reply["result"]["changes"] == [] and "disabled" in reply["result"]["skipped"]
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_status_reports_the_setting_and_what_learning_has_touched(userdata):
    make_node(userdata / "nodes", "apples")
    await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.feedback"], {"node_ids": ["apples"], "kind": "up"})
    nl.record_use(["apples"], userdata / "nodes")
    status = (await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.status"], {}))["result"]
    assert status["enabled"] is True and status["halted"] is False
    assert status["nodes_touched"] == 1 and status["recent"], "the recent log is shown"


@pytest.mark.asyncio
async def test_the_toggle_persists_and_rejects_a_non_boolean(userdata):
    off = (await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.set_enabled"], {"enabled": False}))["result"]
    assert off["enabled"] is False and nl.learning_enabled() is False
    on = (await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.set_enabled"], {"enabled": True}))["result"]
    assert on["enabled"] is True and nl.learning_enabled() is True
    assert "error" in await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.set_enabled"], {"enabled": "yes"})


@pytest.mark.asyncio
async def test_reset_needs_an_explicit_confirm(userdata):
    path = make_node(userdata / "nodes", "apples", relevance="0.30")
    nl.record_use(["apples"], userdata / "nodes")
    before = path.read_bytes()
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.reset_weights"], {})
    assert "error" in reply and "confirm" in reply["error"]["message"].lower()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_reset_runs_through_the_gated_tool_registry(userdata, monkeypatch):
    from sdk.agent.tools import ToolRegistry

    seen = []

    async def spy(name, arguments, authority_level="auto", *, principal=None):
        seen.append((name, arguments))
        return {"success": True, "reset": 3}

    monkeypatch.setattr(ToolRegistry, "run_tool", classmethod(lambda cls, *a, **k: spy(*a, **k)))
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.reset_weights"], {"confirm": True})
    assert seen == [("reset_node_weights", {"confirm": True, "node_ids": None})]
    assert reply["result"]["reset"] == 3


@pytest.mark.asyncio
async def test_a_refusal_from_the_gate_is_reported_not_hidden(userdata, monkeypatch):
    from sdk.agent.tools import ToolRegistry

    async def refuse(name, arguments, authority_level="auto", *, principal=None):
        return {"success": False, "error": "no lease"}

    monkeypatch.setattr(ToolRegistry, "run_tool", classmethod(lambda cls, *a, **k: refuse(*a, **k)))
    reply = await call(learning_rpc.LEARNING_RPC_HANDLERS["learning.reset_weights"], {"confirm": True})
    assert "error" in reply and "no lease" in reply["error"]["message"]


@pytest.mark.parametrize("method", METHODS)
def test_the_emergency_halt_refuses_every_learning_method(method, monkeypatch):
    monkeypatch.setattr(halt_gate, "is_halted_fail_closed", lambda: True)
    assert halt_gate.check_rpc(method, {}, 1) is not None


@pytest.mark.parametrize("method", METHODS)
def test_every_learning_method_is_registered_with_the_server(method):
    from python.harness import server

    assert method in server._HANDLERS

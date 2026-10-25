"""
The renderer's way into the LoRA review queue: ``lora.candidates``, ``lora.decide``, ``lora.status``, ``lora.train``,
``lora.rollback`` and ``lora.settings`` (python/harness/lora_handlers.py).

The database is faked and the user-data root is tmp_path; the approval store, the safety filters and the persona registry run for
real. The pipeline's own entry points (start_background, sync_provider_row) are the parent module's and are faked here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from python.harness import halt_gate, lora_handlers
from sdk.agent import lora_pipeline
from sdk.agent import node_learning as nl

REPLY = "I kept the cargo manifest in order, and I would rather tell you the route is risky than pretend it is not."
WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
METHODS = ("lora.candidates", "lora.decide", "lora.status", "lora.train", "lora.rollback", "lora.settings")


def _row(reply=REPLY, prompt="How was the run tonight, Tala?"):
    return {"content": reply, "tool_calls": [], "created_at": WHEN, "prev_role": "user", "prev_content": prompt}


@pytest.fixture
def world(monkeypatch, tmp_path):
    from python.harness import db
    from sdk.agent import documentation_nodes

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    monkeypatch.setattr(documentation_nodes, "resolve_active_nodes_dir", lambda: nodes)
    state = {"rows": [_row()], "halted": False}

    async def fake_fetch(query, *args):
        return state["rows"]

    monkeypatch.setattr(db, "fetch", fake_fetch)
    monkeypatch.setattr(halt_gate, "is_halted_fail_closed", lambda: state["halted"])
    return type("World", (), {"state": state, "tmp": tmp_path})


async def call(method, params=None):
    return json.loads(await lora_handlers.LORA_RPC_HANDLERS[method](params if params is not None else {}, 7))


def test_every_method_is_registered_and_none_is_allowed_through_the_halt():
    assert set(lora_handlers.LORA_RPC_HANDLERS) == set(METHODS)
    for method in METHODS:
        assert method not in halt_gate.HALTED_STATUS_METHODS, "a halt refuses every one of these at dispatch"


def test_the_server_dispatches_them():
    from python.harness import server

    for method in METHODS:
        assert server._HANDLERS[method] is lora_handlers.LORA_RPC_HANDLERS[method]


# --- lora.candidates ------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_lists_the_queue(world):
    reply = await call("lora.candidates", {"status": "pending", "kind": "chat", "limit": 10})

    result = reply["result"]
    assert result["total"] == 1 and result["items"][0]["reply"] == REPLY and result["counts"]["pending"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"status": "maybe"}, {"kind": "video"}, {"limit": 0}, {"limit": "ten"}, {"offset": -1}, {"limit": 9999}])
async def test_candidates_refuses_bad_filters_without_raising(world, params):
    result = (await call("lora.candidates", params))["result"]

    assert result["success"] is False and result["error"]


# --- lora.decide ----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_approves_and_the_queue_shows_it(world):
    item_id = (await call("lora.candidates"))["result"]["items"][0]["id"]

    result = (await call("lora.decide", {"ids": [item_id], "decision": "approved"}))["result"]

    assert result["success"] is True and result["updated"] == 1 and result["refused"] == {}
    assert (await call("lora.candidates"))["result"]["counts"]["approved"] == 1


@pytest.mark.asyncio
async def test_decide_reports_what_it_refused(world):
    result = (await call("lora.decide", {"ids": ["nope"], "decision": "approved"}))["result"]

    assert result["success"] is True and result["updated"] == 0 and result["refused"] == {"nope": "no such item"}


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"ids": ["a"], "decision": "maybe"},
    {"ids": "a", "decision": "approved"},
    {"ids": [1, 2], "decision": "approved"},
    {"ids": [], "decision": "approved"},
    {"decision": "approved"},
    {"ids": ["a"] * 501, "decision": "approved"},
])
async def test_decide_validates_its_input_without_raising(world, params):
    result = (await call("lora.decide", params))["result"]

    assert result["success"] is False and result["error"]


@pytest.mark.asyncio
async def test_decide_is_refused_while_halted(world):
    item_id = (await call("lora.candidates"))["result"]["items"][0]["id"]
    world.state["halted"] = True

    result = (await call("lora.decide", {"ids": [item_id], "decision": "approved"}))["result"]
    world.state["halted"] = False

    assert result["success"] is False and result["halted"] is True
    assert (await call("lora.candidates"))["result"]["counts"]["approved"] == 0


# --- lora.status ----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_pulls_the_pipeline_the_persona_the_counts_and_the_settings_together(world):
    result = (await call("lora.status"))["result"]

    assert set(result) >= {"pipeline", "persona", "counts", "settings"}
    assert result["counts"]["pending"] == 1
    assert result["persona"]["active"] is None and result["persona"]["entries"] == []
    assert result["settings"] == {"auto_train": False, "routing": "off"}
    assert "last_status" in result["pipeline"]


@pytest.mark.asyncio
async def test_status_still_answers_when_the_database_is_down(world, monkeypatch):
    from python.harness import db

    async def boom(query, *args):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "fetch", boom)

    result = (await call("lora.status"))["result"]

    assert result["counts"] is None and "database is down" in result["counts_error"]
    assert result["persona"]["adapters"] == 0


# --- lora.settings --------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_writes_the_two_keys_the_pipeline_and_the_router_read(world):
    result = (await call("lora.settings", {"auto_train": True, "routing": "in_character"}))["result"]

    assert result == {"success": True, "settings": {"auto_train": True, "routing": "in_character"}}
    config = nl.read_config()
    assert config["lora_auto_train"] is True and config["lora_routing"] == "in_character"
    assert (await call("lora.settings"))["result"]["settings"] == {"auto_train": True, "routing": "in_character"}


@pytest.mark.asyncio
async def test_settings_keeps_whatever_else_learning_json_holds(world):
    nl.write_config({"lora_hour": 3, "enabled": True})

    await call("lora.settings", {"routing": "fallback"})

    assert nl.read_config()["lora_hour"] == 3 and nl.read_config()["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"routing": "always"}, {"routing": 3}, {"auto_train": "yes"}, {"auto_train": 1}])
async def test_settings_refuses_a_bad_value_and_writes_nothing(world, params):
    result = (await call("lora.settings", params))["result"]

    assert result["success"] is False and result["error"]
    assert "lora_routing" not in nl.read_config() and "lora_auto_train" not in nl.read_config()


@pytest.mark.asyncio
async def test_settings_is_refused_while_halted(world):
    world.state["halted"] = True

    result = (await call("lora.settings", {"auto_train": True}))["result"]
    world.state["halted"] = False

    assert result["success"] is False and result["halted"] is True and "lora_auto_train" not in nl.read_config()


# --- lora.train -----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_train_starts_the_pipeline_in_the_background_and_returns_at_once(world, monkeypatch):
    seen = {}

    async def fake_start(**kwargs):
        seen.update(kwargs)
        return {"status": "started"}

    monkeypatch.setattr(lora_pipeline, "start_background", fake_start, raising=False)

    result = (await call("lora.train"))["result"]

    assert result == {"started": True, "reason": "started"}
    assert seen == {"reason": "ui", "force": True}


@pytest.mark.asyncio
async def test_train_says_why_it_did_not_start(world, monkeypatch):
    async def busy(**kwargs):
        return {"status": "busy", "reason": "a training run is already in progress"}

    monkeypatch.setattr(lora_pipeline, "start_background", busy, raising=False)

    result = (await call("lora.train"))["result"]

    assert result == {"started": False, "reason": "a training run is already in progress"}


@pytest.mark.asyncio
async def test_train_is_refused_while_halted_and_never_reaches_the_pipeline(world, monkeypatch):
    async def must_not_run(**kwargs):
        raise AssertionError("the pipeline was started while halted")

    monkeypatch.setattr(lora_pipeline, "start_background", must_not_run, raising=False)
    world.state["halted"] = True

    result = (await call("lora.train"))["result"]
    world.state["halted"] = False

    assert result["started"] is False and result["halted"] is True


@pytest.mark.asyncio
async def test_train_answers_with_an_error_not_an_exception_when_the_pipeline_breaks(world, monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(lora_pipeline, "start_background", broken, raising=False)

    reply = await call("lora.train")

    assert "error" in reply and "boom" in reply["error"]["message"]


# --- lora.rollback --------------------------------------------------------------------------------------------------------------


def _adapters(world, n=3):
    from python.inference import persona_adapter as pa

    for i in range(1, n + 1):
        gguf = world.tmp / f"a-0{i}.gguf"
        gguf.write_bytes(b"GGUF" + b"\0" * 5000)
        pa.add_adapter({"id": f"a-0{i}", "created": f"2026-10-0{i}T02:00:00", "gguf": str(gguf), "scores": {}})
        pa.activate(f"a-0{i}")


@pytest.mark.asyncio
async def test_rollback_restores_the_previous_adapter_stops_the_server_and_syncs_the_provider(world, monkeypatch):
    from python.inference import persona_adapter as pa

    order = []

    async def fake_stop():
        order.append("stop")

    async def fake_sync():
        order.append("sync")

    monkeypatch.setattr(pa, "stop", fake_stop)
    monkeypatch.setattr(lora_pipeline, "sync_provider_row", fake_sync, raising=False)
    _adapters(world)

    result = (await call("lora.rollback"))["result"]

    assert result == {"success": True, "active": "a-02"} and pa.active_entry()["id"] == "a-02"
    assert order == ["stop", "sync"], "the old server goes first, so the next turn serves the restored adapter"


@pytest.mark.asyncio
async def test_rollback_with_nothing_earlier_switches_the_persona_off(world, monkeypatch):
    from python.inference import persona_adapter as pa

    async def noop():
        return None

    monkeypatch.setattr(pa, "stop", noop)
    monkeypatch.setattr(lora_pipeline, "sync_provider_row", lambda: None, raising=False)  # a plain function works too
    _adapters(world, n=1)

    result = (await call("lora.rollback"))["result"]

    assert result == {"success": True, "active": None} and pa.active_entry() is None


@pytest.mark.asyncio
async def test_rollback_is_refused_while_halted(world, monkeypatch):
    from python.inference import persona_adapter as pa

    _adapters(world)
    world.state["halted"] = True

    result = (await call("lora.rollback"))["result"]
    world.state["halted"] = False

    assert result["success"] is False and result["halted"] is True and pa.active_entry()["id"] == "a-03"

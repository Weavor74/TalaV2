"""
Node learning is reachable: the pieces exist, and something calls each of them.

The failure mode of this codebase is well-built subsystems nothing invokes (the goal engine's process_feedback and
dream_cycle sat unused). These tests pin the call sites as well as the behavior.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sdk.agent import lease_policy
from sdk.agent import node_learning as nl
from sdk.agent.parser import parse_file

REPO = Path(__file__).resolve().parents[1]


def read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def make_node(directory, node_id, relevance="0.80", confidence="0.80", extra=""):
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
    data-weight-governance-importance="0.50"{extra}>
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


# --- the admin tool ------------------------------------------------------------------------------------------------


def test_the_reset_tool_is_registered_with_a_schema():
    from sdk.agent.tools import ToolRegistry

    entry = {t["name"]: t for t in ToolRegistry.get_tools()}["reset_node_weights"]
    assert {"node_ids", "confirm"} <= set(entry["schema"]["properties"])
    assert "admin" in entry["description"].lower()


@pytest.mark.asyncio
async def test_the_reset_tool_refuses_without_a_confirm(userdata):
    from sdk.agent.learning_tools import reset_node_weights

    path = make_node(userdata / "nodes", "apples", relevance="0.30",
                     extra='\n    data-usage-count="2"\n    data-last-used="2026-10-01T00:00:00+00:00"')
    before = path.read_bytes()
    result = await reset_node_weights(confirm=False)
    assert result["success"] is False and "confirm" in result["error"]
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_the_reset_tool_resets_learned_nodes_when_confirmed(userdata):
    from sdk.agent.learning_tools import reset_node_weights

    path = make_node(userdata / "nodes", "apples", relevance="0.30", confidence="0.40",
                     extra='\n    data-usage-count="2"\n    data-last-used="2026-10-01T00:00:00+00:00"')
    result = await reset_node_weights(confirm=True)
    assert result["success"] is True and result["nodes_reset"] == 1
    node = parse_file(str(path))
    assert (node.weight_relevance, node.weight_confidence, node.usage_count) == (1.0, 1.0, 0)


@pytest.mark.parametrize("tool", ["reset_node_weights", "write_memory"])
def test_tools_that_write_node_files_are_lease_gated(tool):
    """Both write a node file, so both are in the lease policy's file-edit class like every other node writer."""
    assert lease_policy._classify_tool(tool) == "file_edit"


def test_weight_updates_are_a_local_ledger_kind():
    """The shared chain is for interactions with someone else; a weight change is private activity."""
    from python.harness import chains

    assert chains.is_external("node-weight-update", "") is False
    assert "node-weight-update" in chains.LOCAL_KINDS


# --- one write path --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_old_feedback_helper_now_moves_the_weights_retrieval_reads(userdata):
    """adjust_behavioral_weights used to edit a node-metadata JSON block 358 of 361 nodes lack and retrieval never reads."""
    from sdk.agent.tools import adjust_behavioral_weights

    path = make_node(userdata / "nodes", "apples")
    result = await adjust_behavioral_weights(str(path), "that answer was wrong and inaccurate")
    node = parse_file(str(path))
    assert node.weight_confidence < 0.80, "confidence dropped on the attribute retrieval reads"
    assert "node-metadata" not in path.read_text(encoding="utf-8"), "no legacy JSON block was bolted on"
    assert "confidence" in result.lower()


@pytest.mark.asyncio
async def test_positive_feedback_text_raises_confidence(userdata):
    from sdk.agent.tools import adjust_behavioral_weights

    path = make_node(userdata / "nodes", "apples")
    await adjust_behavioral_weights(str(path), "great, that was exactly right")
    assert parse_file(str(path)).weight_confidence > 0.80


@pytest.mark.asyncio
async def test_feedback_with_no_clear_sentiment_changes_nothing(userdata):
    from sdk.agent.tools import adjust_behavioral_weights

    path = make_node(userdata / "nodes", "apples")
    before = path.read_bytes()
    await adjust_behavioral_weights(str(path), "hmm okay")
    assert path.read_bytes() == before


# --- the call sites -----------------------------------------------------------------------------------------------------


def test_the_turn_pipeline_counts_a_use_when_nodes_are_injected():
    server = read("python", "harness", "server.py")
    injected = server.index("Injected neural context from nodes")
    assert "node_learning.record_use(injected_node_ids" in server[injected:injected + 900]


def test_an_explicit_remember_is_saved_before_retrieval_runs():
    server = read("python", "harness", "server.py")
    start = server.index("raw_user_prompt = prompt")
    assert "remember_from_message(raw_user_prompt)" in server[start:start + 600]
    assert start < server.index("retrieve_relevant_nodes(retrieval_query")


def test_the_reply_tells_the_ui_which_nodes_it_used():
    server = read("python", "harness", "server.py")
    tail = server[server.index('"fallback_notice": fallback_notice,'):][:300]
    assert '"injected_node_ids": injected_node_ids' in tail and '"remembered_node_id"' in tail


def test_the_server_registers_the_learning_rpcs():
    server = read("python", "harness", "server.py")
    assert "**LEARNING_RPC_HANDLERS" in server and "from python.harness.learning_rpc import LEARNING_RPC_HANDLERS" in server


def test_the_heartbeat_tick_calls_the_dream():
    tick = read("python", "harness", "heartbeat_scheduler.py")
    body = tick[tick.index("async def _evaluate_tick"):]
    assert re.search(r"from sdk\.agent\.dreaming import dream_if_due", body) and "await dream_if_due()" in body

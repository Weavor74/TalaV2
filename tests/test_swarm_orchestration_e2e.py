"""
tests/test_swarm_orchestration_e2e.py
======================================
End-to-end regression proving a swarm topology run actually executes and
reaches completion against the real DB — not mocked at the orchestration
layer.

Before this fix, `SwarmEngine.spawn_topology` / `evaluate_edges` only ever
wrote 'pending' rows into the `tasks` table (`sdk/agent/swarm_engine.py`).
Nothing ever consumed them: `evaluate_edges` was never called from any
production code path, so a started swarm sat at "pending" forever. The
existing unit tests (`sdk/agent/tests/test_swarm_engine.py`) only exercised
each function in isolation with `BatchQueue`/`db` fully mocked, so they
never would have caught this — they proved the SQL/logic looked right, not
that a swarm ever actually ran to completion.

This test drives a real 2-node topology through `SwarmEngine.spawn_topology`
and polls the real Postgres dev DB until the parent task either completes
or times out. Node execution itself (`DAGExecutionEngine`, which needs a
live LLM provider + git worktree plumbing) and the final synthesis LLM call
are mocked — this test's job is to prove the graph-progression wiring
(spawn -> execute node -> evaluate_edges -> enqueue+execute next node ->
... -> terminal leaf -> synthesize -> parent COMPLETE) is real, not to
grade any particular model's output.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, patch

from python.harness import db
from python.harness.db import close_pool, init_pool
from sdk.agent.swarm_engine import SwarmEngine
from sdk.agent.topology import SwarmTopology, TopologyEdge, TopologyNode, TopologyRegistry

TOPOLOGY_ID = "test-e2e-two-node-swarm"


def _register_topology() -> SwarmTopology:
    topology = SwarmTopology(
        topology_id=TOPOLOGY_ID,
        name="E2E Test Two-Node Swarm",
        nodes=[
            TopologyNode(id="alpha", role="worker", description="Do the first thing"),
            TopologyNode(id="beta", role="worker", description="Do the second thing"),
        ],
        edges=[TopologyEdge(from_node="alpha", to_node="beta", condition="success")],
        failure_policy={},
    )
    TopologyRegistry.register(topology)
    return topology


async def _wait_for_status(task_id: str, target_statuses: set[str], timeout: float = 15.0) -> str | None:
    """Poll the real DB until task_id reaches one of target_statuses or timeout."""
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        row = await db.fetchrow("SELECT status FROM tasks WHERE id = $1", task_id)
        if row:
            last_status = row["status"]
            if last_status in target_statuses:
                return last_status
        await asyncio.sleep(0.05)
    return last_status


@pytest.mark.asyncio
async def test_swarm_topology_runs_to_completion_end_to_end():
    """
    Regression for the "nothing ever executes a swarm node" bug: spawns a
    real 2-node topology and confirms it self-drives to a terminal COMPLETE
    state with both nodes actually executed and a synthesis result recorded
    on the parent task — against the real DB, no mocking of BatchQueue/db.
    """
    await init_pool()
    try:
        _register_topology()
        goal_text = "E2E swarm wiring test goal"

        with patch(
            "sdk.agent.graph_engine.DAGExecutionEngine.execute_task_as_dag",
            new_callable=AsyncMock,
        ) as mock_dag, patch(
            "sdk.agent.synthesizer.SynthesizerWorker.synthesize",
            new_callable=AsyncMock,
        ) as mock_synth:
            mock_dag.return_value = {"success": True, "output": "node did real work", "error": None}
            mock_synth.return_value = {"success": True, "output": "final synthesized report"}

            parent_id = await SwarmEngine.spawn_topology(goal_text, TOPOLOGY_ID)
            final_status = await _wait_for_status(parent_id, {"complete", "failed", "blocked"}, timeout=15.0)

            assert final_status == "complete", (
                f"swarm parent task never reached completion (last status={final_status!r}) — "
                "the graph did not self-drive to the terminal node"
            )
            assert mock_dag.await_count == 2, "expected exactly 2 node executions (alpha, beta)"
            mock_synth.assert_awaited_once()

        # Both nodes should have actually run and landed as 'complete'.
        children = await db.fetch("SELECT id, status, payload FROM tasks WHERE parent_id = $1", parent_id)
        assert len(children) == 2
        node_ids = set()
        for c in children:
            assert c["status"] == "complete", f"node task {c['id']} did not complete: {c['status']}"
            payload = c["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            node_ids.add(payload.get("node_id"))
        assert node_ids == {"alpha", "beta"}

        parent_row = await db.fetchrow("SELECT result FROM tasks WHERE id = $1", parent_id)
        result = parent_row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        assert result.get("output") == "final synthesized report"
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_swarm_topology_gate_failure_routes_to_blocked():
    """
    Regression for the retry/route_to path: confirms a failing gate with no
    configured failure_policy halts that branch instead of silently hanging,
    and that a requires_human policy actually blocks the parent task.
    """
    await init_pool()
    try:
        blocked_topology_id = "test-e2e-blocked-swarm"
        topology = SwarmTopology(
            topology_id=blocked_topology_id,
            name="E2E Test Blocked Swarm",
            nodes=[
                TopologyNode(id="gatekeeper", role="verifier", description="Gate that always fails"),
                TopologyNode(id="unreachable", role="worker", description="Should never run"),
            ],
            edges=[TopologyEdge(from_node="gatekeeper", to_node="unreachable", condition="approved")],
            failure_policy={
                "approved": {"requires_human": True},
            },
        )
        TopologyRegistry.register(topology)

        with patch(
            "sdk.agent.graph_engine.DAGExecutionEngine.execute_task_as_dag",
            new_callable=AsyncMock,
        ) as mock_dag:
            # "approved" gate checks for LGTM/APPROVED in the output — this output has neither.
            mock_dag.return_value = {"success": True, "output": "Rejected: needs work", "error": None}

            parent_id = await SwarmEngine.spawn_topology("E2E blocked swarm goal", blocked_topology_id)
            final_status = await _wait_for_status(parent_id, {"complete", "failed", "blocked"}, timeout=15.0)

            assert final_status == "blocked", f"expected parent to halt at 'blocked', got {final_status!r}"
            # Only the single gatekeeper node should have run — 'unreachable' must never execute.
            assert mock_dag.await_count == 1
    finally:
        await close_pool()

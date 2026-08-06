"""
test_swarm_engine.py - Auto-generated documentation base.
"""

from __future__ import annotations

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock

from sdk.agent.swarm_engine import SwarmEngine, ZombieReaper
from sdk.agent.topology import TopologyRegistry, SwarmTopology, TopologyNode, TopologyEdge
from sdk.agent.queue import BatchQueue, TaskStatus

@pytest.fixture
def dummy_topology():
    node1 = TopologyNode(id="node_start", role="researcher", description="Start node")
    node2 = TopologyNode(id="node_end", role="writer", description="End node")
    edge = TopologyEdge(from_node="node_start", to_node="node_end", condition="default")
    topology = SwarmTopology(
        topology_id="test_topo",
        name="Test Topology",
        nodes=[node1, node2],
        edges=[edge],
        failure_policy={}
    )
    TopologyRegistry.register(topology)
    yield topology
    # Cleanup logic if registry has unregister (not strictly required for isolated test mock)

@pytest.mark.asyncio
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
async def test_spawn_topology(mock_enqueue, dummy_topology):
    parent_id = await SwarmEngine.spawn_topology("Test goal", "test_topo")
    assert isinstance(parent_id, str)
    assert len(parent_id) > 0
    # Should enqueue 2 times: once for parent, once for the start node
    assert mock_enqueue.call_count == 2
    
    # Check parent enqueue
    call_args_parent = mock_enqueue.call_args_list[0][1]
    assert call_args_parent["task_id"] == parent_id
    assert call_args_parent["payload"]["topology_id"] == "test_topo"

    # Check start node enqueue
    call_args_node = mock_enqueue.call_args_list[1][1]
    assert call_args_node["payload"]["node_id"] == "node_start"
    assert call_args_node["parent_id"] == parent_id

@pytest.mark.asyncio
@patch("python.harness.db.fetch", new_callable=AsyncMock)
@patch("sdk.agent.gated_verifier.GatedVerifier.verify", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
async def test_evaluate_edges_success(mock_enqueue, mock_verify, mock_fetch, dummy_topology):
    # Mock db.fetch for load topology
    mock_fetch.return_value = [{"payload": json.dumps({"topology_id": "test_topo"})}]
    
    # Mock verify to pass
    mock_verify.return_value = (True, "Good", "default")
    
    await SwarmEngine.evaluate_edges("parent_123", "node_start", {"output": "result"})
    
    # Should enqueue the next node
    assert mock_enqueue.call_count == 1
    call_args = mock_enqueue.call_args_list[0][1]
    assert call_args["payload"]["node_id"] == "node_end"
    assert call_args["parent_id"] == "parent_123"

@pytest.mark.asyncio
@patch("python.harness.db.fetch", new_callable=AsyncMock)
@patch("python.harness.db.execute", new_callable=AsyncMock)
@patch("sdk.agent.gated_verifier.GatedVerifier.verify", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
async def test_evaluate_terminal_node(mock_transition, mock_verify, mock_execute, mock_fetch, dummy_topology):
    # Mock db.fetch for load topology and checking active count
    mock_fetch.side_effect = [
        [{"payload": json.dumps({"topology_id": "test_topo", "goal": "Finish"})}] , # Topology fetch
        [{"c": 0}], # active_count fetch
        [{"payload": json.dumps({"node_id": "node_start"}), "result": {"output": "A", "success": True}}] # results fetch
    ]
    
    with patch("sdk.agent.synthesizer.SynthesizerWorker.synthesize", new_callable=AsyncMock) as mock_synth:
        mock_synth.return_value = {"final": "done"}
        await SwarmEngine.evaluate_edges("parent_123", "node_end", {"output": "result"})
        
        # Verify it synthesized
        mock_synth.assert_called_once()
        
        # Verify the synthesis result was passed straight to transition_task.
        # Regression: evaluate_edges used to write the synthesis result via a
        # raw db.execute() call and *then* call transition_task with no
        # `result` kwarg — transition_task's own UPDATE defaults `result` to
        # {}, so that second write silently clobbered the first, and the
        # parent task's persisted result was always {}. Fixed by passing
        # final_synthesis straight into transition_task's `result` kwarg
        # instead of writing it separately beforehand.
        mock_transition.assert_called_with(
            "parent_123", TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE, result={"final": "done"}
        )


@pytest.mark.asyncio
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
async def test_execute_node_task_skips_git_worktree(mock_transition):
    """
    Swarm nodes must not spin up a per-node git worktree/branch — they run
    directly against the shared local workspace instead, so nodes in the
    same swarm run see each other's file writes and execution has no git
    dependency. DAGExecutionEngine only attempts its own DB-lookup + auto
    `git worktree add` (via handle_task_create_worktree) when a task has no
    worktree_path already set — this proves execute_node_task always sets
    one before handing off, short-circuiting that path.
    """
    from sdk.agent.graph_engine import DAGExecutionEngine

    captured: dict = {}

    async def fake_execute_task_as_dag(self, task, policy=None):
        captured["worktree_path"] = getattr(task, "worktree_path", None)
        return {"success": True, "output": "ok", "error": None}

    with patch.object(DAGExecutionEngine, "execute_task_as_dag", new=fake_execute_task_as_dag), \
         patch("python.harness.server.handle_task_create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch.object(SwarmEngine, "evaluate_edges", new_callable=AsyncMock):
        await SwarmEngine.execute_node_task("task-x", "parent-x", "node-x", "worker", "do a thing")

    assert captured.get("worktree_path"), "worktree_path must be pre-set so DAGExecutionEngine skips auto git-worktree creation"
    assert captured["worktree_path"] == DAGExecutionEngine().root_dir
    mock_create_wt.assert_not_called()

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
        
        # Verify it updated the DB result and transitioned the task
        assert mock_execute.call_count > 0
        mock_transition.assert_called_with("parent_123", TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE)

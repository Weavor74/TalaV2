import os
import pytest
import json
from typing import Any
from unittest.mock import patch, MagicMock
from pathlib import Path

from sdk.agent.graph_engine import DAGExecutionEngine, GraphNode
from sdk.agent.planner import TaskItem
from sdk.agent.queue import BatchQueue
from python.harness.db import init_pool, close_pool
from python.harness import db


async def _reset_task(task_id: str) -> None:
    """Drop any leftover row (and cascaded graph_nodes/events) from a prior
    run of this same fixed task_id, so DAG execution can't short-circuit on
    stale 'already completed' state."""
    await db.execute("DELETE FROM tasks WHERE id = $1", task_id)

def test_graph_node_initialization():
    node = GraphNode(
        id="node-1",
        task_id="task-1",
        node_type="research",
        description="test research",
        dependencies=[],
        inputs={"query": "test"},
        success_criteria={"min_sources": 1}
    )
    assert node.id == "node-1"
    assert node.status == "pending"
    assert node.retry_count == 0

@pytest.mark.asyncio
async def test_db_save_and_load(tmp_path):
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    
    node = GraphNode(
        id="node-1",
        task_id="task-1",
        node_type="research",
        description="test research",
        dependencies=[],
        inputs={"query": "test"},
        success_criteria={"min_sources": 1}
    )
    
    # Save node in-memory/DB
    await engine.save_node(node)
    
    # Load nodes
    loaded = await engine.load_nodes("task-1")
    assert len(loaded) == 1
    assert loaded[0].id == "node-1"
    assert loaded[0].node_type == "research"
    assert loaded[0].inputs == {"query": "test"}

@pytest.mark.asyncio
async def test_dag_execution_success(tmp_path):
    # Synthesis routes through handle_inference_generate, which gates on the
    # real token ledger — the DB pool must be live for the DAG to run at all.
    await init_pool()
    try:
        engine = DAGExecutionEngine(root_dir=str(tmp_path))

        # Create target scratch dir
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(exist_ok=True)

        task = TaskItem(description="Write a run function to scratch/run.py")
        task.task_id = "test-task-success"
        task.payload = {
            "context": {
                "path": "scratch/run.py"
            }
        }
        # graph_nodes.task_id has a FK on tasks.id — a real row must exist before
        # the DAG persists nodes for it.
        await _reset_task(task.task_id)
        await BatchQueue.enqueue_task(task.task_id, task.description, payload=task.payload)

        # Mock Fleet worktree and port allocation; no sandbox patching needed (runs locally)
        with patch("python.harness.server.handle_task_create_worktree", return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path)}})), \
             patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=14000), \
             patch("sdk.agent.port_router.PortRouter.release_port", return_value=True):

            res = await engine.execute_task_as_dag(task)

            # Should run research -> synthesis -> writing -> validation
            assert res["success"] is True
            assert (scratch_dir / "run.py").exists()
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_validation_failure_triggers_rollback_and_retry(tmp_path):
    await init_pool()
    try:
        engine = DAGExecutionEngine(root_dir=str(tmp_path))

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(exist_ok=True)

        task = TaskItem(description="Write buggy code to scratch/buggy.py")
        task.task_id = "test-task-retry"
        task.payload = {
            "context": {
                "path": "scratch/buggy.py"
            }
        }
        await _reset_task(task.task_id)
        await BatchQueue.enqueue_task(task.task_id, task.description, payload=task.payload)

        # Mock validation node to fail on first attempt, then succeed on retry
        original_execute_node = engine._execute_node

        attempts = {}

        async def mock_execute_node(node: GraphNode, nodes: dict[str, GraphNode], backups: dict[str, str | None], worktree_path: str, allocated_port: int) -> bool:
            if node.node_type == "validation":
                attempts[node.id] = attempts.get(node.id, 0) + 1
                if attempts[node.id] == 1:
                    node.outputs = {"error": "Simulated syntax error on first validation run"}
                    return False
            return await original_execute_node(node, nodes, backups, worktree_path, allocated_port)

        with patch.object(engine, "_execute_node", side_effect=mock_execute_node), \
             patch("python.harness.server.handle_task_create_worktree", return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path)}})), \
             patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=14001), \
             patch("sdk.agent.port_router.PortRouter.release_port", return_value=True):

            res = await engine.execute_task_as_dag(task)

            # Validation failed once, rolled back, retried synthesis and writing, then succeeded!
            assert res["success"] is True
            assert attempts.get(f"validation-test-task-retry") == 2
    finally:
        await close_pool()

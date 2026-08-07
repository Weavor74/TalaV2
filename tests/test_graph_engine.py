import os
import pytest
import json
from typing import Any
from unittest.mock import patch, MagicMock, AsyncMock
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

@pytest.mark.asyncio
async def test_execute_task_as_dag_always_unregisters_active_task(tmp_path):
    """
    Regression: execute_task_as_dag used to call register_active_task on
    entry but never unregister_active_task on any exit path (success,
    failure, or exception) — sdk.agent.goal_engine._ACTIVE_TASK_IDS grew
    and never shrank, which permanently blocks
    ZombieReaper.reclaim_zombies (sdk/agent/swarm_engine.py) from ever
    reclaiming that task_id again, since it treats any id in that set as
    "still actively executing".

    The real DAG body (_execute_task_as_dag_inner) is mocked out here —
    this test's job is proving the register/unregister bookkeeping around
    it is airtight, on both the success and the exception path, not
    re-testing DAG execution itself (already covered elsewhere in this
    file with a live DB/LLM).
    """
    from sdk.agent.goal_engine import get_active_task_ids

    engine = DAGExecutionEngine(root_dir=str(tmp_path))

    # Success path
    task_ok = TaskItem(description="noop")
    task_ok.task_id = "test-active-task-ok"
    with patch.object(
        DAGExecutionEngine, "_execute_task_as_dag_inner",
        new_callable=AsyncMock, return_value={"success": True, "output": "ok"},
    ):
        res = await engine.execute_task_as_dag(task_ok)
    assert res["success"] is True
    assert "test-active-task-ok" not in get_active_task_ids()

    # Exception path — must still unregister even when the DAG body blows up.
    task_boom = TaskItem(description="boom")
    task_boom.task_id = "test-active-task-boom"

    async def _boom(self, task, policy=None):
        raise RuntimeError("simulated crash mid-DAG")

    with patch.object(DAGExecutionEngine, "_execute_task_as_dag_inner", new=_boom):
        with pytest.raises(RuntimeError):
            await engine.execute_task_as_dag(task_boom)
    assert "test-active-task-boom" not in get_active_task_ids()


@pytest.mark.asyncio
async def test_validation_node_does_not_claim_tests_ran_when_none_specified(tmp_path):
    """
    Regression: the validation node's success message used to unconditionally
    say "Compilation and test verification passed successfully" even when
    test_file was never set -- which is the normal case for a freeform chat
    goal, since test_file only ever comes from a structured context field
    nothing populates from natural language. A goal that explicitly asked
    for tests to be written and run could report full success with zero
    tests ever having executed (confirmed live: a real chat goal asking for
    a calculator implementation *and* a test file reported "Goal completed
    successfully" while the test file was never created and no test ever
    ran). The output must say plainly whether tests actually ran.
    """
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    target = tmp_path / "output.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    node = GraphNode(
        id="validation-test",
        task_id="test-validation-honesty",
        node_type="validation",
        description="validate",
        inputs={"target_file": str(target), "test_file": None},
    )

    ok = await engine._execute_node(node, {}, {}, str(tmp_path), 0)

    assert ok is True
    assert node.outputs.get("tests_ran") is False
    assert "no automated tests were run" in node.outputs["output"].lower()
    assert "test verification passed" not in node.outputs["output"].lower()


@pytest.mark.asyncio
async def test_validation_node_reports_real_test_success_distinctly(tmp_path):
    """Companion to the above: when a test_file genuinely is specified and
    genuinely passes, the message should say so -- distinct wording from the
    no-test-ran path, so the two cases can never be confused by anything
    reading node.outputs["output"] downstream (chat UI, self-documenter)."""
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    target = tmp_path / "output.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file = tmp_path / "test_output.py"
    test_file.write_text(
        "from output import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    node = GraphNode(
        id="validation-test-2",
        task_id="test-validation-honesty-2",
        node_type="validation",
        description="validate",
        inputs={"target_file": str(target), "test_file": str(test_file)},
    )

    ok = await engine._execute_node(node, {}, {}, str(tmp_path), 0)

    assert ok is True
    assert "tests_ran" not in node.outputs or node.outputs.get("tests_ran") is not False
    assert "test suite verification passed" in node.outputs["output"].lower()


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

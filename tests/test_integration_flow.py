import os
import pytest
import json
from unittest.mock import patch
from pathlib import Path

from sdk.agent.queue import GoalTreeDecomposer, BatchQueue
from sdk.agent.planner import TaskItem
from sdk.agent.graph_engine import DAGExecutionEngine
from python.harness.db import init_pool, close_pool
from python.harness import db

@pytest.mark.asyncio
async def test_tot_graph_fleet_unified_workflow(tmp_path):
    # Setup test workspace directories
    root_dir = tmp_path / "workspace"
    root_dir.mkdir(parents=True, exist_ok=True)
    worktree_dir = tmp_path / "worktree_dir"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    await init_pool()
    try:
        # 1. Initialize decomposer
        decomposer = GoalTreeDecomposer()

        # Mock ToT shortlist generation & task mapping
        user_goal = "Design a low-latency cache system"

        # Ensure ToT shortlist file is clean
        shortlist_file = root_dir / "tot_shortlist.json"
        if shortlist_file.exists():
            os.remove(shortlist_file)

        # 2. Decompose should intercept and run ToT
        # We patch local file operations to run relative to our temp root_dir
        with patch("os.getcwd", return_value=str(root_dir)), \
             patch("python.harness.server.handle_task_create_worktree", return_value=json.dumps({"result": {"success": True, "worktree_path": str(worktree_dir)}})), \
             patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=13000), \
             patch("sdk.agent.port_router.PortRouter.release_port", return_value=True) as mock_port_release:

            # Mock handle_inference_generate for ToT ideation only — the mocked
            # text isn't valid shortlist JSON, so ToT's own deterministic
            # fallback kicks in and still produces tasks.
            with patch("python.harness.server.handle_inference_generate", return_value='{"result": {"text": "mocked approach"}}'):
                tasks = await decomposer.decompose(user_goal)

            # Verify ToT run triggered & tasks mapped
            assert len(tasks) > 0
            # Check shortlist json written
            assert os.path.exists("tot_shortlist.json")

            # 3. Execute the first task through the real DAG engine. Synthesis
            # routes through the real handle_inference_generate (local model
            # fallback chain) and the real token ledger, so no mock here.
            tasks[0].payload = {
                "context": {
                    "path": "cache_module.py"
                }
            }
            # ToT assigns task_id deterministically from the goal text, so a
            # prior run of this same goal leaves "already completed" graph_nodes
            # behind — drop them so the DAG actually executes instead of
            # short-circuiting on stale state.
            await db.execute("DELETE FROM tasks WHERE id = $1", tasks[0].task_id)
            await BatchQueue.enqueue_task(tasks[0].task_id, tasks[0].description, payload=tasks[0].payload)

            graph_engine = DAGExecutionEngine(root_dir=str(root_dir))
            res = await graph_engine.execute_task_as_dag(tasks[0])

            # 4. Assert workflow output & merge
            assert res["success"] is True
            # Check that target file was merged back to the root working directory
            merged_file = root_dir / "cache_module.py"
            assert merged_file.exists()

            # Check that cleanup was triggered
            assert mock_port_release.called
    finally:
        await close_pool()

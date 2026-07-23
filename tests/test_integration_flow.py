import os
import pytest
import json
from unittest.mock import patch, MagicMock
from pathlib import Path

from sdk.agent.queue import GoalTreeDecomposer
from sdk.agent.goal_engine import GoalEngine
from sdk.agent.planner import TaskItem
from sdk.agent.graph_engine import DAGExecutionEngine

@pytest.mark.asyncio
async def test_tot_graph_fleet_unified_workflow(tmp_path):
    # Setup test workspace directories
    root_dir = tmp_path / "workspace"
    root_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Initialize decomposer and engine
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
         patch("python.harness.server.handle_task_create_worktree", return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path / "worktree_dir")}})), \
         patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=13000), \
         patch("sdk.agent.sandbox.DockerSandbox.start_container", return_value=True), \
         patch("sdk.agent.sandbox.DockerSandbox.execute_command", return_value={"success": True, "stdout": "test passed"}) as mock_sb_exec, \
         patch("sdk.agent.sandbox.DockerSandbox.stop_container", return_value=True) as mock_sb_stop, \
         patch("sdk.agent.port_router.PortRouter.release_port", return_value=True) as mock_port_release:
             
        # Mock handle_inference_generate to bypass LLM calls during tests
        with patch("python.harness.server.handle_inference_generate", return_value='{"result": {"text": "mocked approach"}}'):
            tasks = await decomposer.decompose(user_goal)
            
            # Verify ToT run triggered & tasks mapped
            assert len(tasks) > 0
            # Check shortlist json written
            assert os.path.exists("tot_shortlist.json")
            
            # 3. Initialize GoalEngine & execute the task
            engine = GoalEngine(root_dir=str(root_dir))
            
            # Run the tasks using GoalEngine
            # We mock the DB transactions to avoid test DB connection issues
            with patch("python.harness.db.execute", new_callable=AsyncMock) as mock_db_exec:
                # Setup specific payload context path for write node
                tasks[0].payload = {
                    "context": {
                        "path": "cache_module.py"
                    }
                }
                
                # Run the goal engine execution
                graph_engine = DAGExecutionEngine(root_dir=str(root_dir))
                res = await graph_engine.execute_task_as_dag(tasks[0])
                
                # 4. Assert workflow output & merge
                assert res["success"] is True
                # Check that target file was merged back to the root working directory
                merged_file = root_dir / "cache_module.py"
                assert merged_file.exists()
                
                # Check that cleanup was triggered
                assert mock_sb_stop.called
                assert mock_port_release.called

# Helper AsyncMock for db calls
class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)

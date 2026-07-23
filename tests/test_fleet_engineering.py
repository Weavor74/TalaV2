import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import json

from sdk.agent.port_router import PortRouter
from sdk.agent.sandbox import DockerSandbox
from sdk.agent.orchestrator_cli import get_active_tasks, get_port_allocations, show_status, pause_task, resume_task, terminate_task
from sdk.agent.graph_engine import DAGExecutionEngine, GraphNode
from sdk.agent.planner import TaskItem

@pytest.mark.asyncio
async def test_port_router_allocation():
    router = PortRouter(base_port=12000)
    
    # Check physical occupation mock
    with patch.object(router, "is_port_physically_occupied", side_effect=lambda p: p == 12000):
        # 12000 is occupied, so it should allocate 12001
        port = await router.allocate_port("test-task-port")
        assert port == 12001

        # Allocating again for same task returns the same port
        port2 = await router.allocate_port("test-task-port")
        assert port2 == 12001

        # Allocating for a different task gets next port 12002 (since 12001 is now in memory map)
        port3 = await router.allocate_port("another-task")
        assert port3 == 12002

        # Clean up
        await router.release_port("test-task-port")
        await router.release_port("another-task")

@pytest.mark.asyncio
async def test_docker_sandbox_fallback():
    sandbox = DockerSandbox()
    
    # Mock Docker unavailable
    with patch.object(sandbox, "check_docker_available", return_value=False):
        c_name = sandbox.get_container_name("task-abc")
        assert c_name == "tala-sandbox-task-abc"
        
        # Test start_container fails / returns false
        started = await sandbox.start_container("task-abc", "scratch/worktrees/task-abc")
        assert started is False

        # Test execute_command falls back to host execution
        with patch("sdk.agent.async_shell.AsyncShellExecutor.run", return_value={"success": True, "stdout": "host-output"}) as mock_run:
            res = await sandbox.execute_command("task-abc", "echo hello")
            assert res["success"] is True
            assert mock_run.called

@pytest.mark.asyncio
async def test_docker_sandbox_commands():
    sandbox = DockerSandbox()
    
    # Mock Docker available
    with patch.object(sandbox, "check_docker_available", return_value=True), \
         patch("sdk.agent.async_shell.AsyncShellExecutor.run_exec") as mock_run_exec:
        
        # 1. Start container
        mock_run_exec.side_effect = [
            {"success": False, "stdout": ""}, # inspect state running
            {"success": True, "stdout": ""},  # rm -f
            {"success": True, "stdout": ""}   # run -d
        ]
        started = await sandbox.start_container("task-abc", "scratch/worktrees/task-abc", {9000: 9000})
        assert started is True
        assert mock_run_exec.call_count == 3
        # Check command run args
        args = mock_run_exec.call_args_list[2][0][1]
        assert "run" in args
        assert "tala-sandbox-task-abc" in args
        assert "127.0.0.1:9000:9000" in args

        # 2. Execute command
        mock_run_exec.reset_mock()
        mock_run_exec.side_effect = [
            {"success": True, "stdout": "true"}, # inspect state running
            {"success": True, "stdout": "container-output"} # exec
        ]
        res = await sandbox.execute_command("task-abc", "pytest")
        assert res["success"] is True
        args = mock_run_exec.call_args_list[1][0][1]
        assert "exec" in args
        assert "tala-sandbox-task-abc" in args
        assert "pytest" in args

@pytest.mark.asyncio
async def test_orchestrator_cli_dashboard():
    # Verify dashboard and CLI functions execute/mock correctly
    with patch("sdk.agent.orchestrator_cli.get_active_tasks", return_value=[{"id": "t-1", "description": "test task", "status": "running", "worktree_path": "/path/t-1", "branch_name": "t-1-branch"}]), \
         patch("sdk.agent.orchestrator_cli.get_port_allocations", return_value={"t-1": 9000}), \
         patch("sdk.agent.sandbox.DockerSandbox.check_docker_available", return_value=True), \
         patch("sdk.agent.async_shell.AsyncShellExecutor.run_exec", return_value={"success": True, "stdout": "tala-sandbox-t-1 | Up 2 minutes"}):
        
        # Test status printing works without crash
        await show_status()

    # Test actions
    with patch("sdk.agent.sandbox.DockerSandbox.pause_container", return_value=True) as mock_pause, \
         patch("python.harness.db.execute") as mock_db:
        await pause_task("t-1")
        assert mock_pause.called
        assert mock_db.called

@pytest.mark.asyncio
async def test_graph_engine_fleet_integration(tmp_path):
    # Test that execute_task_as_dag runs worktree spinup, port allocation, and sandbox
    engine = DAGExecutionEngine(root_dir=str(tmp_path))
    task = TaskItem(description="Write test to scratch/test_integrate.py")
    task.task_id = "test-fleet-integrate"
    
    # Mock handlers
    with patch("python.harness.server.handle_task_create_worktree", return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path)}})) as mock_wt, \
         patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=9005) as mock_port, \
         patch("sdk.agent.sandbox.DockerSandbox.start_container", return_value=True) as mock_sb_start, \
         patch("sdk.agent.sandbox.DockerSandbox.execute_command", return_value={"success": True, "stdout": "compilation passed"}) as mock_sb_exec:
             
        res = await engine.execute_task_as_dag(task)
        assert res["success"] is True
        assert mock_wt.called
        assert mock_port.called
        assert mock_sb_start.called
        # Check command run inside sandbox
        assert mock_sb_exec.called

@pytest.mark.asyncio
async def test_swarm_fleet_rpc_endpoints():
    from python.harness.server import (
        handle_swarm_fleet_status,
        handle_swarm_fleet_pause,
        handle_swarm_fleet_resume,
        handle_swarm_fleet_terminate
    )

    # 1. Test status RPC
    with patch("sdk.agent.orchestrator_cli.get_active_tasks", return_value=[{"id": "t-1", "description": "test task", "status": "running", "worktree_path": "/path/t-1", "branch_name": "t-1-branch"}]), \
         patch("sdk.agent.orchestrator_cli.get_port_allocations", return_value={"t-1": 9000}), \
         patch("sdk.agent.sandbox.DockerSandbox.check_docker_available", return_value=True), \
         patch("sdk.agent.async_shell.AsyncShellExecutor.run_exec", return_value={"success": True, "stdout": "tala-sandbox-t-1 | Up 2 minutes"}):
             
        res_str = await handle_swarm_fleet_status({}, req_id="test-status")
        res = json.loads(res_str)
        assert res["result"]["success"] is True
        assert len(res["result"]["tasks"]) == 1
        assert res["result"]["tasks"][0]["id"] == "t-1"
        assert res["result"]["tasks"][0]["port"] == 9000
        assert res["result"]["tasks"][0]["container_status"] == "Up 2 minutes"

    # 2. Test pause RPC
    with patch("sdk.agent.orchestrator_cli.pause_task", return_value=True) as mock_pause:
        res_str = await handle_swarm_fleet_pause({"task_id": "t-1"}, req_id="test-pause")
        res = json.loads(res_str)
        assert res["result"]["success"] is True
        assert mock_pause.called

    # 3. Test resume RPC
    with patch("sdk.agent.orchestrator_cli.resume_task", return_value=True) as mock_resume:
        res_str = await handle_swarm_fleet_resume({"task_id": "t-1"}, req_id="test-resume")
        res = json.loads(res_str)
        assert res["result"]["success"] is True
        assert mock_resume.called

    # 4. Test terminate RPC
    with patch("sdk.agent.orchestrator_cli.terminate_task", return_value=True) as mock_term:
        res_str = await handle_swarm_fleet_terminate({"task_id": "t-1"}, req_id="test-term")
        res = json.loads(res_str)
        assert res["result"]["success"] is True
        assert mock_term.called

@pytest.mark.asyncio
async def test_tool_registry_argument_filtering():
    from sdk.agent.tools import ToolRegistry

    # Register a test tool that does not accept keyword argument "content"
    @ToolRegistry.register(name="test_filter_tool", description="Test filtering")
    def test_filter_tool(arg_a: str) -> str:
        return f"a:{arg_a}"

    # Try invoking it with extra arguments (e.g. content and another_phantom)
    args = {"arg_a": "hello", "content": "phantom_content", "another_phantom": 123}
    res = await ToolRegistry.run_tool("test_filter_tool", args)
    
    # Assert execution succeeds and extra arguments were filtered out
    assert res["success"] is True
    assert res["output"] == "a:hello"

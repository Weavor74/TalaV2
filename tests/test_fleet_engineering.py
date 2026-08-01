import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import json

from sdk.agent.port_router import PortRouter
from sdk.agent.sandbox import LocalSandbox, DockerSandbox  # DockerSandbox is an alias for LocalSandbox
from sdk.agent.orchestrator_cli import get_active_tasks, get_port_allocations, show_status, pause_task, resume_task, terminate_task
from sdk.agent.graph_engine import DAGExecutionEngine, GraphNode
from sdk.agent.planner import TaskItem
from sdk.agent.queue import BatchQueue
from python.harness.db import init_pool, close_pool
from python.harness import db

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
async def test_local_sandbox_execute():
    """LocalSandbox always runs commands directly on the host."""
    sandbox = LocalSandbox()

    # start/stop/pause/resume are no-ops that return True
    assert await sandbox.start_container("t1", "/tmp") is True
    assert await sandbox.stop_container("t1") is True
    assert await sandbox.pause_container("t1") is True
    assert await sandbox.resume_container("t1") is True

    # execute_command delegates to AsyncShellExecutor.run
    with patch("sdk.agent.async_shell.AsyncShellExecutor.run",
               return_value={"success": True, "stdout": "host-output"}) as mock_run:
        res = await sandbox.execute_command("t1", "echo hello", cwd="/tmp")
        assert res["success"] is True
        assert mock_run.called

@pytest.mark.asyncio
async def test_orchestrator_cli_dashboard():
    """Dashboard executes without crash and prints task/port info."""
    with patch("sdk.agent.orchestrator_cli.get_active_tasks", return_value=[
        {"id": "t-1", "description": "test task", "status": "running",
         "worktree_path": "/path/t-1", "branch_name": "t-1-branch"}
    ]), \
         patch("sdk.agent.orchestrator_cli.get_port_allocations", return_value={"t-1": 9000}):
        await show_status()  # should not raise

@pytest.mark.asyncio
async def test_orchestrator_cli_pause_resume():
    with patch("python.harness.db.execute") as mock_db:
        await pause_task("t-1")
        assert mock_db.called
        mock_db.reset_mock()
        await resume_task("t-1")
        assert mock_db.called

@pytest.mark.asyncio
async def test_graph_engine_fleet_integration(tmp_path):
    """execute_task_as_dag runs worktree spinup and port allocation, no Docker."""
    await init_pool()
    try:
        engine = DAGExecutionEngine(root_dir=str(tmp_path))
        task = TaskItem(description="Write test to scratch/test_integrate.py")
        task.task_id = "test-fleet-integrate"
        # Drop any leftover row from a prior run of this fixed task_id so the
        # DAG can't short-circuit on stale "already completed" state.
        await db.execute("DELETE FROM tasks WHERE id = $1", task.task_id)
        await BatchQueue.enqueue_task(task.task_id, task.description)

        with patch("python.harness.server.handle_task_create_worktree",
                   return_value=json.dumps({"result": {"success": True, "worktree_path": str(tmp_path)}})) as mock_wt, \
             patch("sdk.agent.port_router.PortRouter.allocate_port", return_value=9005) as mock_port:

            res = await engine.execute_task_as_dag(task)
            assert res["success"] is True
            assert mock_wt.called
            assert mock_port.called
    finally:
        await close_pool()

@pytest.mark.asyncio
async def test_swarm_fleet_rpc_endpoints():
    from python.harness.server import (
        handle_swarm_fleet_status,
        handle_swarm_fleet_pause,
        handle_swarm_fleet_resume,
        handle_swarm_fleet_terminate
    )

    # 1. Test status RPC
    with patch("sdk.agent.orchestrator_cli.get_active_tasks", return_value=[
        {"id": "t-1", "description": "test task", "status": "running",
         "worktree_path": "/path/t-1", "branch_name": "t-1-branch"}
    ]), \
         patch("sdk.agent.orchestrator_cli.get_port_allocations", return_value={"t-1": 9000}):

        res_str = await handle_swarm_fleet_status({}, req_id="test-status")
        res = json.loads(res_str)
        assert res["result"]["success"] is True
        assert len(res["result"]["tasks"]) == 1
        assert res["result"]["tasks"][0]["id"] == "t-1"
        assert res["result"]["tasks"][0]["port"] == 9000
        # No container_status field — Docker is gone
        assert "container_status" not in res["result"]["tasks"][0]

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

@pytest.mark.asyncio
async def test_dynamic_xml_payload_mapping():
    from sdk.agent.tools import ToolRegistry
    import re

    # Register mock self_improvement.dry_run_goal in ToolRegistry
    @ToolRegistry.register(name="self_improvement.dry_run_goal", description="Mock dry run")
    async def mock_dry_run_goal(goal: str, target_files: list | None = None) -> dict:
        return {"goal_received": goal}

    # Simulate parsing in server.py
    generated_text = "<call:self_improvement.dry_run_goal>Clean all nodes</call:self_improvement.dry_run_goal>"
    tool_match = re.search(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', generated_text, re.DOTALL)
    assert tool_match is not None

    tool_name = tool_match.group(1)
    inner_content = tool_match.group(3)

    attrs = {}
    if inner_content:
        inner_content = inner_content.strip()
        mapped = False
        tool_entry = ToolRegistry._tools.get(tool_name.lower())
        if tool_entry:
            func = tool_entry.get("func")
            if func:
                import inspect
                sig = inspect.signature(func)
                params = sig.parameters
                if "goal" in params:
                    attrs["goal"] = inner_content
                    mapped = True

    assert mapped is True
    assert attrs["goal"] == "Clean all nodes"

    # Invoke it via ToolRegistry
    res = await ToolRegistry.run_tool(tool_name, attrs)
    assert res["success"] is True
    assert res["output"]["goal_received"] == "Clean all nodes"

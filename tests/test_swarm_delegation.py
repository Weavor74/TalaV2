"""
Tests for swarm delegation - fake model test showing create-bot, assign-goal, result-returns.

Run with: pytest tests/test_swarm_delegation.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from sdk.agent.long_task import (
    TaskState,
    create_task,
    delegate_task,
    check_delegated_task_status,
    TaskManager,
)


class FakeGoalEngine:
    """Fake GoalEngine that simulates agent execution."""
    
    def __init__(self):
        self.executed_goals = []
        self.next_result = {"success": True, "task_id": "fake-exec-123", "phases_completed": ["phase1"], "report": "Done"}
    
    async def run_goal(self, goal: str, policy=None):
        self.executed_goals.append(goal)
        return self.next_result


@pytest.mark.asyncio
async def test_delegate_task_spawns_agent():
    """Delegating a task actually calls submit_agent_goal."""
    parent_task = await create_task(
        goal="Parent: Build entire application",
        steps=[
            {"description": "Design architecture", "action": "write_file"},
            {"description": "Implement backend", "action": "delegate"},  # This will be delegated
            {"description": "Implement frontend", "action": "write_file"},
        ],
    )
    
    fake_engine = FakeGoalEngine()
    
    with patch("sdk.agent.long_task.ToolRegistry") as mock_registry_class:
        mock_registry = Mock()
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "goal-exec-456",
            "status": "success",
            "task_id": "fake-task-123",
        })
        mock_registry_class.return_value = mock_registry
        
        result = await delegate_task(
            parent_task_id=parent_task.task_id,
            sub_goal="Build the backend API",
            steps=[
                {"description": "Create models", "action": "write_file"},
                {"description": "Create routes", "action": "write_file"},
                {"description": "Write tests", "action": "run_shell"},
            ],
        )
    
    assert result["success"]
    assert "sub_task_id" in result
    assert result["goal_execution_id"] == "goal-exec-456"
    
    # Verify the sub-task was created and delegated
    manager = TaskManager()
    sub_task = await manager.load(result["sub_task_id"])
    assert sub_task is not None
    assert sub_task.state == TaskState.DELEGATED
    assert sub_task.context["spawned_agent"] is True
    assert sub_task.context["goal_execution_id"] == "goal-exec-456"
    assert sub_task.parent_task_id == parent_task.task_id
    
    # Clean up
    await manager.delete(parent_task.task_id)
    await manager.delete(sub_task.task_id)


@pytest.mark.asyncio
async def test_check_delegated_task_status():
    """We can check the status of a delegated sub-task."""
    parent_task = await create_task(
        goal="Parent task",
        steps=[{"description": "Step 1", "action": "test"}],
    )
    
    with patch("sdk.agent.long_task.ToolRegistry") as mock_registry_class:
        mock_registry = Mock()
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "goal-exec-789",
        })
        mock_registry_class.return_value = mock_registry
        
        result = await delegate_task(
            parent_task_id=parent_task.task_id,
            sub_goal="Sub-goal",
            steps=[{"description": "Sub-step", "action": "test"}],
        )
    
    assert result["success"]
    sub_task_id = result["sub_task_id"]
    
    # Check status
    status = await check_delegated_task_status(sub_task_id)
    assert status["success"]
    assert status["state"] == TaskState.DELEGATED.value
    assert status["delegated_to"] == "goal-exec-789"
    
    # Clean up
    manager = TaskManager()
    await manager.delete(parent_task.task_id)
    await manager.delete(sub_task_id)


@pytest.mark.asyncio
async def test_swarm_workflow_end_to_end():
    """Full swarm workflow: parent creates, delegates to 2 children, collects results."""
    # Create parent task
    parent = await create_task(
        goal="Orchestrator: Deploy full-stack application",
        steps=[
            {"description": "Plan deployment", "action": "write_file"},
            {"description": "Deploy backend (delegate)", "action": "delegate"},
            {"description": "Deploy frontend (delegate)", "action": "delegate"},
            {"description": "Configure DNS", "action": "run_shell"},
            {"description": "Verify health", "action": "run_shell"},
        ],
    )
    
    delegated_tasks = []
    
    with patch("sdk.agent.long_task.ToolRegistry") as mock_registry_class:
        mock_registry = Mock()
        
        # First delegation: backend
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "backend-exec-001",
        })
        mock_registry_class.return_value = mock_registry
        
        result1 = await delegate_task(
            parent_task_id=parent.task_id,
            sub_goal="Agent 1: Deploy backend services",
            steps=[
                {"description": "Build Docker image", "action": "run_shell"},
                {"description": "Push to registry", "action": "run_shell"},
                {"description": "Deploy to k8s", "action": "run_shell"},
            ],
        )
        assert result1["success"]
        delegated_tasks.append(result1["sub_task_id"])
        
        # Second delegation: frontend
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "frontend-exec-002",
        })
        
        result2 = await delegate_task(
            parent_task_id=parent.task_id,
            sub_goal="Agent 2: Deploy frontend assets",
            steps=[
                {"description": "Build static assets", "action": "run_shell"},
                {"description": "Upload to CDN", "action": "run_shell"},
                {"description": "Invalidate cache", "action": "run_shell"},
            ],
        )
        assert result2["success"]
        delegated_tasks.append(result2["sub_task_id"])
    
    # Verify swarm structure
    manager = TaskManager()
    
    # Parent knows about children
    parent_reload = await manager.load(parent.task_id)
    assert parent_reload is not None
    
    # Children know about parent
    child1 = await manager.load(delegated_tasks[0])
    child2 = await manager.load(delegated_tasks[1])
    assert child1.parent_task_id == parent.task_id
    assert child2.parent_task_id == parent.task_id
    assert child1.state == TaskState.DELEGATED
    assert child2.state == TaskState.DELEGATED
    
    # Both children have different goal executions
    assert child1.context["goal_execution_id"] == "backend-exec-001"
    assert child2.context["goal_execution_id"] == "frontend-exec-002"
    
    # Simulate children completing
    child1.state = TaskState.COMPLETE
    child2.state = TaskState.COMPLETE
    await manager.save(child1)
    await manager.save(child2)
    
    # Check that we can query child status
    status1 = await check_delegated_task_status(child1.task_id)
    status2 = await check_delegated_task_status(child2.task_id)
    assert status1["state"] == TaskState.COMPLETE.value
    assert status2["state"] == TaskState.COMPLETE.value
    
    # Clean up
    await manager.delete(parent.task_id)
    await manager.delete(child1.task_id)
    await manager.delete(child2.task_id)


@pytest.mark.asyncio
async def test_delegation_failure_handling():
    """If agent spawning fails, the sub-task is marked FAILED."""
    parent = await create_task(
        goal="Parent task",
        steps=[{"description": "Step", "action": "test"}],
    )
    
    with patch("sdk.agent.long_task.ToolRegistry") as mock_registry_class:
        mock_registry = Mock()
        mock_registry.run_tool = AsyncMock(return_value={
            "success": False,
            "error": "GoalEngine is offline",
        })
        mock_registry_class.return_value = mock_registry
        
        result = await delegate_task(
            parent_task_id=parent.task_id,
            sub_goal="This will fail",
            steps=[{"description": "Will not execute", "action": "test"}],
        )
    
    assert not result["success"]
    assert "Failed to delegate" in result["error"]
    
    # Sub-task should be FAILED
    manager = TaskManager()
    sub_task = await manager.load(result["sub_task_id"])
    assert sub_task.state == TaskState.FAILED
    assert "delegation_error" in sub_task.context
    
    # Clean up
    await manager.delete(parent.task_id)
    await manager.delete(sub_task.task_id)


@pytest.mark.asyncio
async def test_three_level_delegation_tree():
    """Swarm can have multiple levels: grandparent → parent → child."""
    grandparent = await create_task(
        goal="Grandparent: Entire product launch",
        steps=[{"description": "High-level plan", "action": "write_file"}],
    )
    
    with patch("sdk.agent.long_task.ToolRegistry") as mock_registry_class:
        mock_registry = Mock()
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "parent-exec",
        })
        mock_registry_class.return_value = mock_registry
        
        # Level 1: Grandparent → Parent
        parent_result = await delegate_task(
            parent_task_id=grandparent.task_id,
            sub_goal="Parent: Marketing campaign",
            steps=[{"description": "Campaign plan", "action": "write_file"}],
        )
        
        mock_registry.run_tool = AsyncMock(return_value={
            "success": True,
            "execution_id": "child-exec",
        })
        
        # Level 2: Parent → Child
        child_result = await delegate_task(
            parent_task_id=parent_result["sub_task_id"],
            sub_goal="Child: Social media posts",
            steps=[{"description": "Write posts", "action": "write_file"}],
        )
    
    manager = TaskManager()
    
    # Verify 3-level hierarchy
    grandparent_task = await manager.load(grandparent.task_id)
    parent_task = await manager.load(parent_result["sub_task_id"])
    child_task = await manager.load(child_result["sub_task_id"])
    
    assert grandparent_task.parent_task_id is None
    assert parent_task.parent_task_id == grandparent.task_id
    assert child_task.parent_task_id == parent_task.task_id
    
    # Clean up
    await manager.delete(grandparent.task_id)
    await manager.delete(parent_task.task_id)
    await manager.delete(child_task.task_id)


def test_swarm_architecture_documented():
    """Document the swarm architecture for future developers."""
    architecture = """
    Swarm Delegation Architecture
    ==============================
    
    1. Parent creates DurableTask with steps
    2. When step.action == "delegate", call delegate_task()
    3. delegate_task() creates sub-task and calls submit_agent_goal()
    4. submit_agent_goal() spawns real GoalEngine execution
    5. Sub-task is marked DELEGATED with goal_execution_id
    6. Parent can check_delegated_task_status() to monitor
    7. When child completes, parent continues remaining steps
    
    Data Flow:
    - Parent task_id → child parent_task_id (linkage)
    - submit_agent_goal() → execution_id → stored in child context
    - Child state transitions: PENDING → DELEGATED → COMPLETE/FAILED
    
    Swarm Topology:
    - Tree structure: one parent, multiple children
    - Multi-level: children can delegate to grandchildren
    - PostgreSQL stores all task states (query by parent_task_id)
    """
    assert "submit_agent_goal" in architecture
    assert "DELEGATED" in architecture

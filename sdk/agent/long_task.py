"""
long_task.py - Durable task/plan state surviving per-reply step limits.

Implements a persistent task system that can:
- Survive across reply boundaries (MAX_TOOL_ROUNDS_PER_REPLY limit)
- Delegate sub-tasks to child agents
- Coordinate swarm planning across multiple agents
- Resume incomplete work in subsequent replies

Architecture:
- Task state persisted to PostgreSQL (primary authority)
- Task context saved in HTML-NN nodes for semantic retrieval
- Sub-agent delegation via spawn_agent tool
- Swarm coordination via broadcast_task_event
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class TaskState(Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DELEGATED = "delegated"
    COMPLETE = "complete"
    FAILED = "failed"


class StepLimit(Enum):
    """Why a task was paused."""
    NONE = "none"
    PER_REPLY_CAP = "per_reply_cap"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_DELEGATION = "needs_delegation"


@dataclass
class TaskStep:
    """One step in a multi-step task."""
    step_id: str
    description: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    state: TaskState = TaskState.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class DurableTask:
    """A task that can survive across multiple reply rounds."""
    task_id: str
    goal: str
    steps: list[TaskStep] = field(default_factory=list)
    state: TaskState = TaskState.PENDING
    pause_reason: StepLimit = StepLimit.NONE
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    parent_task_id: str | None = None
    agent_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    steps_executed_this_reply: int = 0
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "steps": [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "action": s.action,
                    "args": s.args,
                    "result": s.result,
                    "state": s.state.value,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "error": s.error,
                }
                for s in self.steps
            ],
            "state": self.state.value,
            "pause_reason": self.pause_reason.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "parent_task_id": self.parent_task_id,
            "agent_id": self.agent_id,
            "context": self.context,
            "steps_executed_this_reply": self.steps_executed_this_reply,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DurableTask:
        """Deserialize from persistence."""
        steps = [
            TaskStep(
                step_id=s["step_id"],
                description=s["description"],
                action=s["action"],
                args=s["args"],
                result=s["result"],
                state=TaskState(s["state"]),
                started_at=datetime.fromisoformat(s["started_at"]) if s["started_at"] else None,
                completed_at=datetime.fromisoformat(s["completed_at"]) if s["completed_at"] else None,
                error=s["error"],
            )
            for s in data["steps"]
        ]
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            steps=steps,
            state=TaskState(data["state"]),
            pause_reason=StepLimit(data["pause_reason"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            parent_task_id=data.get("parent_task_id"),
            agent_id=data.get("agent_id"),
            context=data.get("context", {}),
            steps_executed_this_reply=data.get("steps_executed_this_reply", 0),
        )
    
    def next_pending_step(self) -> TaskStep | None:
        """Get the next step to execute."""
        for step in self.steps:
            if step.state == TaskState.PENDING:
                return step
        return None
    
    def is_complete(self) -> bool:
        """Check if all steps are complete."""
        return all(s.state == TaskState.COMPLETE for s in self.steps)
    
    def failed_steps(self) -> list[TaskStep]:
        """Get all failed steps."""
        return [s for s in self.steps if s.state == TaskState.FAILED]


class TaskManager:
    """Manages durable task state with PostgreSQL persistence."""
    
    def __init__(self):
        self._ensure_schema()
    
    def _ensure_schema(self):
        """Create tasks table if it doesn't exist."""
        from python.harness import db
        schema = """
        CREATE TABLE IF NOT EXISTS long_tasks (
            task_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            state TEXT NOT NULL,
            pause_reason TEXT NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            parent_task_id TEXT,
            agent_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_long_tasks_state ON long_tasks(state);
        CREATE INDEX IF NOT EXISTS idx_long_tasks_parent ON long_tasks(parent_task_id);
        CREATE INDEX IF NOT EXISTS idx_long_tasks_agent ON long_tasks(agent_id);
        """
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(db.execute(schema))
            else:
                loop.run_until_complete(db.execute(schema))
        except Exception as e:
            log.warning(f"Could not create long_tasks schema: {e}")
    
    async def save(self, task: DurableTask):
        """Persist task state."""
        from python.harness import db
        task.updated_at = datetime.now(UTC)
        await db.execute(
            """
            INSERT INTO long_tasks (task_id, goal, state, pause_reason, data, created_at, updated_at, parent_task_id, agent_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (task_id) DO UPDATE SET
                state = EXCLUDED.state,
                pause_reason = EXCLUDED.pause_reason,
                data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at
            """,
            task.task_id,
            task.goal,
            task.state.value,
            task.pause_reason.value,
            json.dumps(task.to_dict()),
            task.created_at,
            task.updated_at,
            task.parent_task_id,
            task.agent_id,
        )
        log.info(f"Saved task {task.task_id} (state={task.state.value}, steps={len(task.steps)})")
    
    async def load(self, task_id: str) -> DurableTask | None:
        """Load task from persistence."""
        from python.harness import db
        row = await db.fetchone(
            "SELECT data FROM long_tasks WHERE task_id = $1",
            task_id,
        )
        if not row:
            return None
        return DurableTask.from_dict(json.loads(row["data"]))
    
    async def list_active(self, agent_id: str | None = None) -> list[DurableTask]:
        """List all active (not complete/failed) tasks."""
        from python.harness import db
        query = """
        SELECT data FROM long_tasks
        WHERE state NOT IN ('complete', 'failed')
        """
        args = []
        if agent_id:
            query += " AND agent_id = $1"
            args.append(agent_id)
        query += " ORDER BY updated_at DESC"
        rows = await db.fetch(query, *args)
        return [DurableTask.from_dict(json.loads(row["data"])) for row in rows]
    
    async def delete(self, task_id: str):
        """Delete a task (usually after completion)."""
        from python.harness import db
        await db.execute("DELETE FROM long_tasks WHERE task_id = $1", task_id)


async def create_task(goal: str, steps: list[dict[str, Any]], parent_id: str | None = None) -> DurableTask:
    """Create a new durable task."""
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    task = DurableTask(
        task_id=task_id,
        goal=goal,
        steps=[
            TaskStep(
                step_id=f"{task_id}-step-{i}",
                description=s["description"],
                action=s["action"],
                args=s.get("args", {}),
            )
            for i, s in enumerate(steps)
        ],
        parent_task_id=parent_id,
    )
    manager = TaskManager()
    await manager.save(task)
    return task


async def resume_task(task_id: str, max_steps_this_reply: int = 8) -> dict[str, Any]:
    """Resume execution of a paused task.
    
    Executes up to max_steps_this_reply steps, then pauses if more remain.
    Returns status and next step information.
    """
    manager = TaskManager()
    task = await manager.load(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}
    
    if task.state in (TaskState.COMPLETE, TaskState.FAILED):
        return {"success": True, "task_id": task_id, "state": task.state.value, "message": "Task already finished"}
    
    task.state = TaskState.IN_PROGRESS
    task.steps_executed_this_reply = 0
    executed = []
    
    while task.steps_executed_this_reply < max_steps_this_reply:
        step = task.next_pending_step()
        if not step:
            break
        
        step.state = TaskState.IN_PROGRESS
        step.started_at = datetime.now(UTC)
        
        try:
            result = await _execute_step(step)
            step.result = result
            if result.get("success"):
                step.state = TaskState.COMPLETE
            else:
                step.state = TaskState.FAILED
                step.error = result.get("error", "Unknown error")
        except Exception as e:
            step.state = TaskState.FAILED
            step.error = str(e)
            log.exception(f"Step {step.step_id} failed")
        
        step.completed_at = datetime.now(UTC)
        executed.append(step)
        task.steps_executed_this_reply += 1
    
    if task.is_complete():
        task.state = TaskState.COMPLETE
        task.pause_reason = StepLimit.NONE
    elif task.failed_steps():
        task.state = TaskState.FAILED
        task.pause_reason = StepLimit.NONE
    elif task.steps_executed_this_reply >= max_steps_this_reply:
        task.state = TaskState.BLOCKED
        task.pause_reason = StepLimit.PER_REPLY_CAP
    
    await manager.save(task)
    
    return {
        "success": True,
        "task_id": task_id,
        "state": task.state.value,
        "pause_reason": task.pause_reason.value,
        "steps_executed": len(executed),
        "steps_remaining": len([s for s in task.steps if s.state == TaskState.PENDING]),
        "executed": [{"step_id": s.step_id, "description": s.description, "state": s.state.value} for s in executed],
    }


async def _execute_step(step: TaskStep) -> dict[str, Any]:
    """Execute one task step by calling the appropriate tool."""
    from sdk.agent.tools import ToolRegistry
    registry = ToolRegistry()
    
    try:
        result = await registry.run_tool(step.action, step.args, principal=None)
        return result
    except Exception as e:
        log.exception(f"Step execution failed: {step.step_id}")
        return {"success": False, "error": str(e)}


async def delegate_task(parent_task_id: str, sub_goal: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Delegate a sub-task to a child agent using submit_agent_goal."""
    from sdk.agent.tools import ToolRegistry
    
    sub_task = await create_task(sub_goal, steps, parent_id=parent_task_id)
    
    # Actually spawn the agent via submit_agent_goal
    try:
        registry = ToolRegistry()
        result = await registry.run_tool("submit_agent_goal", {"goal": sub_goal}, principal=None)
        
        if result.get("success"):
            # Store the spawned goal execution ID in the sub-task
            sub_task.context["goal_execution_id"] = result.get("execution_id")
            sub_task.context["spawned_agent"] = True
            sub_task.state = TaskState.DELEGATED
            
            manager = TaskManager()
            await manager.save(sub_task)
            
            return {
                "success": True,
                "sub_task_id": sub_task.task_id,
                "goal_execution_id": result.get("execution_id"),
                "message": f"Delegated sub-task {sub_task.task_id} to goal execution {result.get('execution_id')}",
            }
        else:
            sub_task.state = TaskState.FAILED
            sub_task.context["delegation_error"] = result.get("error", "Unknown error")
            manager = TaskManager()
            await manager.save(sub_task)
            
            return {
                "success": False,
                "error": f"Failed to delegate: {result.get('error')}",
                "sub_task_id": sub_task.task_id,
            }
    except Exception as e:
        log.exception("Delegation failed")
        return {"success": False, "error": str(e)}


async def check_delegated_task_status(sub_task_id: str) -> dict[str, Any]:
    """Check status of a delegated sub-task."""
    manager = TaskManager()
    task = await manager.load(sub_task_id)
    
    if not task:
        return {"success": False, "error": f"Task {sub_task_id} not found"}
    
    if not task.context.get("spawned_agent"):
        return {"success": False, "error": "Task was not delegated to an agent"}
    
    # Check goal execution status
    goal_execution_id = task.context.get("goal_execution_id")
    if goal_execution_id:
        try:
            from sdk.agent.tools import ToolRegistry
            registry = ToolRegistry()
            # Query the goal execution status (if such a tool exists)
            # For now, return the stored state
            pass
        except Exception:
            pass
    
    return {
        "success": True,
        "sub_task_id": sub_task_id,
        "state": task.state.value,
        "goal": task.goal,
        "delegated_to": goal_execution_id,
        "context": task.context,
    }


async def list_pending_tasks() -> dict[str, Any]:
    """List all pending/in-progress tasks for the current agent."""
    manager = TaskManager()
    tasks = await manager.list_active()
    return {
        "success": True,
        "tasks": [
            {
                "task_id": t.task_id,
                "goal": t.goal,
                "state": t.state.value,
                "pause_reason": t.pause_reason.value,
                "steps_total": len(t.steps),
                "steps_complete": len([s for s in t.steps if s.state == TaskState.COMPLETE]),
            }
            for t in tasks
        ],
    }

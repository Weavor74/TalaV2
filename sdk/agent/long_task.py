"""
Durable task state that survives per-reply tool-round caps.

PostgreSQL is the primary store when the harness pool is up; an in-process
map is the fallback so unit tests and first-run do not require a live DB.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_MEMORY: dict[str, dict[str, Any]] = {}
_SCHEMA_READY = False


class TaskState(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DELEGATED = "delegated"
    COMPLETE = "complete"
    FAILED = "failed"


class StepLimit(Enum):
    NONE = "none"
    PER_REPLY_CAP = "per_reply_cap"
    OVERALL_BUDGET = "overall_budget"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_DELEGATION = "needs_delegation"


@dataclass
class TaskStep:
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
        steps = [
            TaskStep(
                step_id=s["step_id"],
                description=s["description"],
                action=s["action"],
                args=s.get("args") or {},
                result=s.get("result"),
                state=TaskState(s["state"]),
                started_at=datetime.fromisoformat(s["started_at"]) if s.get("started_at") else None,
                completed_at=datetime.fromisoformat(s["completed_at"]) if s.get("completed_at") else None,
                error=s.get("error"),
            )
            for s in data.get("steps") or []
        ]
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            steps=steps,
            state=TaskState(data["state"]),
            pause_reason=StepLimit(data.get("pause_reason") or "none"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(UTC),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(UTC),
            parent_task_id=data.get("parent_task_id"),
            agent_id=data.get("agent_id"),
            context=data.get("context") or {},
            steps_executed_this_reply=int(data.get("steps_executed_this_reply") or 0),
        )

    def next_pending_step(self) -> TaskStep | None:
        for step in self.steps:
            if step.state == TaskState.PENDING:
                return step
        return None

    def is_complete(self) -> bool:
        return bool(self.steps) and all(s.state == TaskState.COMPLETE for s in self.steps)

    def failed_steps(self) -> list[TaskStep]:
        return [s for s in self.steps if s.state == TaskState.FAILED]


def _pool_ready() -> bool:
    try:
        from python.harness import db
        db.get_pool()
        return True
    except Exception:
        return False


async def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY or not _pool_ready():
        return
    from python.harness import db
    await db.execute(
        """
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
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_long_tasks_state ON long_tasks(state)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_long_tasks_parent ON long_tasks(parent_task_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_long_tasks_agent ON long_tasks(agent_id)")
    _SCHEMA_READY = True


class TaskManager:
    async def save(self, task: DurableTask) -> None:
        task.updated_at = datetime.now(UTC)
        payload = task.to_dict()
        _MEMORY[task.task_id] = payload
        if not _pool_ready():
            return
        try:
            await _ensure_schema()
            from python.harness import db
            await db.execute(
                """
                INSERT INTO long_tasks
                    (task_id, goal, state, pause_reason, data, created_at, updated_at, parent_task_id, agent_id)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                ON CONFLICT (task_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    pause_reason = EXCLUDED.pause_reason,
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at,
                    parent_task_id = EXCLUDED.parent_task_id,
                    agent_id = EXCLUDED.agent_id
                """,
                task.task_id,
                task.goal,
                task.state.value,
                task.pause_reason.value,
                json.dumps(payload),
                task.created_at,
                task.updated_at,
                task.parent_task_id,
                task.agent_id,
            )
        except Exception as exc:
            log.warning("long_tasks postgres save failed, using memory: %s", exc)

    async def load(self, task_id: str) -> DurableTask | None:
        if _pool_ready():
            try:
                await _ensure_schema()
                from python.harness import db
                row = await db.fetchrow("SELECT data FROM long_tasks WHERE task_id = $1", task_id)
                if row:
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    return DurableTask.from_dict(data)
            except Exception as exc:
                log.warning("long_tasks postgres load failed: %s", exc)
        raw = _MEMORY.get(task_id)
        return DurableTask.from_dict(raw) if raw else None

    async def list_active(self, agent_id: str | None = None) -> list[DurableTask]:
        if _pool_ready():
            try:
                await _ensure_schema()
                from python.harness import db
                if agent_id:
                    rows = await db.fetch(
                        "SELECT data FROM long_tasks WHERE state NOT IN ('complete', 'failed') AND agent_id = $1 ORDER BY updated_at DESC",
                        agent_id,
                    )
                else:
                    rows = await db.fetch(
                        "SELECT data FROM long_tasks WHERE state NOT IN ('complete', 'failed') ORDER BY updated_at DESC"
                    )
                out = []
                for row in rows:
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    out.append(DurableTask.from_dict(data))
                return out
            except Exception as exc:
                log.warning("long_tasks postgres list failed: %s", exc)
        tasks = [DurableTask.from_dict(v) for v in _MEMORY.values()]
        active = [t for t in tasks if t.state not in (TaskState.COMPLETE, TaskState.FAILED)]
        if agent_id:
            active = [t for t in active if t.agent_id == agent_id]
        active.sort(key=lambda t: t.updated_at, reverse=True)
        return active

    async def load_by_session(self, session_id: str) -> DurableTask | None:
        for task in await self.list_active():
            if task.context.get("session_id") == session_id:
                return task
        return None

    async def delete(self, task_id: str) -> None:
        _MEMORY.pop(task_id, None)
        if not _pool_ready():
            return
        try:
            from python.harness import db
            await db.execute("DELETE FROM long_tasks WHERE task_id = $1", task_id)
        except Exception as exc:
            log.warning("long_tasks postgres delete failed: %s", exc)


async def create_task(
    goal: str,
    steps: list[dict[str, Any]] | None = None,
    parent_id: str | None = None,
) -> DurableTask:
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    task = DurableTask(
        task_id=task_id,
        goal=goal,
        steps=[
            TaskStep(
                step_id=f"{task_id}-step-{i}",
                description=s.get("description") or s.get("action") or f"step {i}",
                action=s.get("action") or "noop",
                args=s.get("args") or {},
            )
            for i, s in enumerate(steps or [])
        ],
        parent_task_id=parent_id,
    )
    await TaskManager().save(task)
    return task


async def _execute_step(step: TaskStep) -> dict[str, Any]:
    from sdk.agent.tools import ToolRegistry
    try:
        result = await ToolRegistry.run_tool(step.action, step.args, principal=None)
        if isinstance(result, dict) and "success" not in result:
            result = {**result, "success": result.get("status") in ("success", None) and not result.get("error")}
        return result if isinstance(result, dict) else {"success": True, "result": result}
    except Exception as exc:
        log.exception("Step execution failed: %s", step.step_id)
        return {"success": False, "error": str(exc)}


async def resume_task(
    task_id: str,
    max_steps_this_reply: int = 8,
    execute_step: Callable[[TaskStep], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    manager = TaskManager()
    task = await manager.load(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}
    if task.state in (TaskState.COMPLETE, TaskState.FAILED):
        return {"success": True, "task_id": task_id, "state": task.state.value, "message": "Task already finished"}

    runner = execute_step or _execute_step
    task.state = TaskState.IN_PROGRESS
    task.steps_executed_this_reply = 0
    executed: list[TaskStep] = []

    while task.steps_executed_this_reply < max_steps_this_reply:
        step = task.next_pending_step()
        if not step:
            break
        step.state = TaskState.IN_PROGRESS
        step.started_at = datetime.now(UTC)
        try:
            result = await runner(step)
            step.result = result
            if result.get("success"):
                step.state = TaskState.COMPLETE
            else:
                step.state = TaskState.FAILED
                step.error = result.get("error") or "Unknown error"
        except Exception as exc:
            step.state = TaskState.FAILED
            step.error = str(exc)
        step.completed_at = datetime.now(UTC)
        executed.append(step)
        task.steps_executed_this_reply += 1

    if task.is_complete():
        task.state = TaskState.COMPLETE
        task.pause_reason = StepLimit.NONE
    elif task.failed_steps() and not task.next_pending_step():
        task.state = TaskState.FAILED
        task.pause_reason = StepLimit.NONE
    elif task.steps_executed_this_reply >= max_steps_this_reply and task.next_pending_step():
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


async def list_pending_tasks() -> dict[str, Any]:
    tasks = await TaskManager().list_active()
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

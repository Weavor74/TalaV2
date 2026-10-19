"""
Live-loop glue for DurableTask.

handle_inference_generate calls attach_long_job / report_long_job_turn.
When a reply hits its per-reply tool-round cap, a job is created (or updated)
automatically, progress is shown to the user, and the next generate for that
session continues the same job until the overall budget is spent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sdk.agent.long_task import DurableTask, StepLimit, TaskManager, TaskState, create_task

log = logging.getLogger(__name__)

DEFAULT_PER_REPLY_CAP = 8
DEFAULT_OVERALL_BUDGET = 24


@dataclass
class LongJobTurn:
    session_id: str
    task: DurableTask | None
    per_reply_cap: int
    inject_user_message: str | None = None


def progress_text(task: DurableTask) -> str:
    ctx = task.context
    used = int(ctx.get("overall_used") or 0)
    budget = int(ctx.get("overall_budget") or DEFAULT_OVERALL_BUDGET)
    remaining = max(0, budget - used)
    return (
        f"\n\n[Long job {task.task_id}: {used}/{budget} tool rounds used, "
        f"{remaining} remaining in the overall budget. "
        f"State={task.state.value}.]"
    )


async def attach_long_job(
    session_id: str,
    user_prompt: str,
    per_reply_cap: int = DEFAULT_PER_REPLY_CAP,
) -> LongJobTurn:
    """Load an in-flight job for this session and continue it automatically."""
    manager = TaskManager()
    task = await manager.load_by_session(session_id)
    if task is None:
        return LongJobTurn(session_id=session_id, task=None, per_reply_cap=per_reply_cap)

    used = int(task.context.get("overall_used") or 0)
    budget = int(task.context.get("overall_budget") or DEFAULT_OVERALL_BUDGET)
    remaining = max(0, budget - used)
    cap = max(1, min(per_reply_cap, remaining or 1))
    task.context["last_user"] = (user_prompt or "")[:400]
    task.state = TaskState.IN_PROGRESS
    await manager.save(task)
    note = (
        "[SYSTEM] Continue the in-progress long job "
        f"{task.task_id} ({used}/{budget} tool rounds used). "
        "Do not restart the work; pick up the unfinished steps."
    )
    return LongJobTurn(session_id=session_id, task=task, per_reply_cap=cap, inject_user_message=note)


async def report_long_job_turn(
    turn: LongJobTurn,
    loop_count: int,
    max_loops: int,
    generated_text: str,
    *,
    find_tool_call: Callable[[str], Any],
    user_prompt: str = "",
) -> str | None:
    """Persist cap/completion and return user-visible progress, or None."""
    capped = loop_count >= max_loops
    still_calling = bool(generated_text and find_tool_call(generated_text))
    manager = TaskManager()
    task = turn.task

    if capped and still_calling:
        if task is None:
            task = await create_task(goal=(user_prompt or "multi-step request")[:400], steps=[])
            task.context = {
                "session_id": turn.session_id,
                "overall_budget": DEFAULT_OVERALL_BUDGET,
                "overall_used": 0,
                "per_reply_cap": max_loops,
            }
            turn.task = task
        used = int(task.context.get("overall_used") or 0) + loop_count
        budget = int(task.context.get("overall_budget") or DEFAULT_OVERALL_BUDGET)
        task.context["overall_used"] = used
        task.state = TaskState.BLOCKED
        if used >= budget:
            task.pause_reason = StepLimit.OVERALL_BUDGET
            await manager.save(task)
            return (
                progress_text(task)
                + "\nI've used the overall tool-round budget for this job. "
                "Ask me to continue in a new job if the remaining work still matters."
            )
        task.pause_reason = StepLimit.PER_REPLY_CAP
        await manager.save(task)
        return (
            progress_text(task)
            + "\nI've reached this reply's step limit. The job is saved; "
            "send another message and I will continue automatically."
        )

    if task is not None and not still_calling:
        task.context["overall_used"] = int(task.context.get("overall_used") or 0) + loop_count
        task.state = TaskState.COMPLETE
        task.pause_reason = StepLimit.NONE
        await manager.save(task)
        return progress_text(task) + "\n✓ Long job finished."
    return None


async def run_reply_tool_rounds(
    final_text: str,
    inference_messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    principal: Any,
    one_completion: Callable[..., Any],
    *,
    max_rounds: int,
    find_tool_call: Callable[[str], Any],
    strip_tags: Callable[[str], str],
    run_tool_round: Callable[..., Any],
    session_id: str | None = None,
    user_prompt: str = "",
) -> str:
    """Room-agent / A2A tool rounds with the same cap-and-continue rules."""
    if not tools:
        return strip_tags(final_text)
    turn = LongJobTurn(session_id=session_id or "", task=None, per_reply_cap=max_rounds)
    if session_id:
        turn = await attach_long_job(session_id, user_prompt, per_reply_cap=max_rounds)
        if turn.inject_user_message:
            inference_messages.append({"role": "user", "content": turn.inject_user_message})
    executed = 0
    for _ in range(turn.per_reply_cap):
        if not final_text:
            break
        tool_response = await run_tool_round(final_text, tools, principal)
        if tool_response is None:
            break
        inference_messages.append({"role": "assistant", "content": final_text})
        inference_messages.append({"role": "user", "content": tool_response})
        final_text = await one_completion()
        executed += 1
    note = await report_long_job_turn(
        turn, executed, turn.per_reply_cap, final_text,
        find_tool_call=find_tool_call, user_prompt=user_prompt,
    )
    if note:
        return (final_text or "") + note
    return final_text

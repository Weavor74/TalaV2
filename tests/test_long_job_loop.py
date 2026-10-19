"""Live reply-loop long job: automatic continue, overall budget, visible progress."""
from __future__ import annotations

from pathlib import Path

import pytest

from python.harness.long_job_loop import (
    DEFAULT_OVERALL_BUDGET,
    attach_long_job,
    report_long_job_turn,
    run_reply_tool_rounds,
)
from sdk.agent.long_task import StepLimit, TaskManager, TaskState


def _call(text: str):
    return text if "<call:" in (text or "") else None


@pytest.mark.asyncio
async def test_cap_creates_job_and_shows_progress():
    turn = await attach_long_job("sess-cap-1", "please implement the whole stack")
    assert turn.task is None
    note = await report_long_job_turn(
        turn, loop_count=8, max_loops=8, generated_text="<call:run_shell command='pytest' />",
        find_tool_call=_call, user_prompt="please implement the whole stack",
    )
    assert note is not None
    assert "Long job" in note
    assert "step limit" in note.lower()
    assert turn.task is not None
    assert turn.task.pause_reason == StepLimit.PER_REPLY_CAP
    await TaskManager().delete(turn.task.task_id)


@pytest.mark.asyncio
async def test_next_reply_continues_automatically():
    first = await attach_long_job("sess-cont-1", "build the release")
    await report_long_job_turn(
        first, 8, 8, "<call:run_shell command='x' />",
        find_tool_call=_call, user_prompt="build the release",
    )
    second = await attach_long_job("sess-cont-1", "ok keep going")
    assert second.task is not None
    assert second.inject_user_message is not None
    assert "Continue the in-progress long job" in second.inject_user_message
    assert second.task.task_id == first.task.task_id
    await TaskManager().delete(second.task.task_id)


@pytest.mark.asyncio
async def test_overall_budget_stops_the_job():
    turn = await attach_long_job("sess-budget-1", "huge job")
    await report_long_job_turn(
        turn, 8, 8, "<call:run_shell command='a' />",
        find_tool_call=_call, user_prompt="huge job",
    )
    turn.task.context["overall_used"] = DEFAULT_OVERALL_BUDGET - 2
    await TaskManager().save(turn.task)
    again = await attach_long_job("sess-budget-1", "continue")
    note = await report_long_job_turn(
        again, again.per_reply_cap, again.per_reply_cap, "<call:run_shell command='b' />",
        find_tool_call=_call, user_prompt="continue",
    )
    assert "overall tool-round budget" in note
    loaded = await TaskManager().load(again.task.task_id)
    assert loaded.pause_reason == StepLimit.OVERALL_BUDGET
    await TaskManager().delete(again.task.task_id)


@pytest.mark.asyncio
async def test_completion_marks_job_done():
    turn = await attach_long_job("sess-done-1", "build it")
    await report_long_job_turn(
        turn, 8, 8, "<call:run_shell command='a' />",
        find_tool_call=_call, user_prompt="build it",
    )
    nxt = await attach_long_job("sess-done-1", "continue")
    note = await report_long_job_turn(
        nxt, 2, 8, "all done, no more tools",
        find_tool_call=_call, user_prompt="continue",
    )
    assert "Long job finished" in note
    loaded = await TaskManager().load(nxt.task.task_id)
    assert loaded.state == TaskState.COMPLETE
    await TaskManager().delete(nxt.task.task_id)


@pytest.mark.asyncio
async def test_run_reply_tool_rounds_strips_when_no_tools():
    async def complete():
        return "plain"

    text = await run_reply_tool_rounds(
        "<call:get_system_info />", [], None, None, complete,
        max_rounds=3, find_tool_call=_call,
        strip_tags=lambda t: "STRIPPED", run_tool_round=lambda *a: None,
    )
    assert text == "STRIPPED"


def test_server_wires_the_live_loop():
    source = Path("python/harness/server.py").read_text(encoding="utf-8")
    generate = source.split("loop_count = 0", 1)[1].split("async def handle_db_query", 1)[0]
    assert "attach_long_job" in generate
    assert "report_long_job_turn" in generate
    assert "run_reply_tool_rounds" in source
    assert "from python.harness.long_job_loop import" in source

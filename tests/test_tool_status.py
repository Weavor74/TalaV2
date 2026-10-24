"""
The live "Worked for Ns ... IN PROGRESS" card in the chat.

Every tool run broadcast "in progress" when it started and never said it had finished, so the card stayed blue, and its timer
kept counting up every second, for as long as the window stayed open -- seen 2026-10-24 as "Worked for 140s" under an answer
that had already arrived, after a reply ran get_system_info. The card is finished by a status of complete / failed.

A burst of tool calls is one card, and it closes shortly after the LAST one finishes -- never mid-run (a 30-minute command must not
read "complete" at minute one), and once, not once per tool.
"""
from __future__ import annotations

import asyncio

import pytest

from sdk.agent import tool_status


@pytest.fixture()
def sent(monkeypatch):
    events: list[dict] = []

    async def fake_broadcast(task_id, phase, status, message, details=None):
        events.append({"task_id": task_id, "phase": phase, "status": status, "message": message, "details": details or {}})

    from sdk.agent.telemetry import LiveStatusReporter

    monkeypatch.setattr(LiveStatusReporter, "broadcast_status", staticmethod(fake_broadcast))
    monkeypatch.setattr(tool_status, "CLOSE_AFTER_S", 0.05)
    tool_status._reset_for_tests()
    yield events
    tool_status._reset_for_tests()


async def settle(seconds=0.2):
    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_starting_a_tool_announces_it_as_before(sent):
    tool_status.started("read_workspace_file", {"path": "a.txt"})
    await settle()
    assert [e["status"] for e in sent] == ["in_progress"]
    first = sent[0]
    assert first["task_id"] == "active_tool" and first["phase"] == "Executing read_workspace_file"
    assert first["details"] == {"action_type": "read_file", "target": "a.txt"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,action", [
    ("write_workspace_file", {"path": "x"}, "write_file"),
    ("search_workspace_files", {"query": "q"}, "search_workspace"),
    ("run_workspace_command", {"command": "ls"}, "run_command"),
    ("get_system_info", {}, "thinking"),
])
async def test_the_action_type_is_worked_out_from_the_tool_name(sent, name, args, action):
    tool_status.started(name, args)
    await settle(0.05)
    assert sent[0]["details"]["action_type"] == action


@pytest.mark.asyncio
async def test_the_card_is_completed_shortly_after_the_last_tool_finishes(sent):
    tool_status.started("get_system_info", {})
    tool_status.finished("get_system_info", ok=True)
    await settle()
    assert [e["status"] for e in sent] == ["in_progress", "complete"]
    assert sent[1]["task_id"] == "active_tool"


@pytest.mark.asyncio
async def test_it_is_not_completed_while_a_tool_is_still_running(sent):
    tool_status.started("run_workspace_command", {"command": "python long.py"})
    await settle(0.3)  # far longer than the close delay
    assert [e["status"] for e in sent] == ["in_progress"], "a running tool must not read as finished"
    tool_status.finished("run_workspace_command", ok=True)
    await settle()
    assert sent[-1]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_burst_of_tools_is_one_card_closed_once(sent):
    for name in ("a_tool", "b_tool", "c_tool"):
        tool_status.started(name, {})
        tool_status.finished(name, ok=True)
        await asyncio.sleep(0.01)  # well inside the close delay, so the next tool continues the same card
    await settle()
    assert [e["status"] for e in sent].count("complete") == 1


@pytest.mark.asyncio
async def test_two_tools_in_parallel_close_the_card_only_when_both_are_done(sent):
    tool_status.started("one", {})
    tool_status.started("two", {})
    tool_status.finished("one", ok=True)
    await settle(0.3)
    assert "complete" not in [e["status"] for e in sent]
    tool_status.finished("two", ok=True)
    await settle()
    assert sent[-1]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_failure_in_the_burst_closes_the_card_as_failed(sent):
    tool_status.started("a_tool", {})
    tool_status.finished("a_tool", ok=False, error="boom")
    await settle()
    assert sent[-1]["status"] == "failed" and "boom" in sent[-1]["message"]


@pytest.mark.asyncio
async def test_the_next_burst_after_a_closed_one_starts_clean(sent):
    tool_status.started("a_tool", {})
    tool_status.finished("a_tool", ok=False, error="boom")
    await settle()
    sent.clear()
    tool_status.started("b_tool", {})
    tool_status.finished("b_tool", ok=True)
    await settle()
    assert sent[-1]["status"] == "complete", "an earlier failure must not colour the next card"


def test_outside_an_event_loop_nothing_raises():
    tool_status._reset_for_tests()
    tool_status.started("a_tool", {})
    tool_status.finished("a_tool", ok=True)


@pytest.mark.asyncio
async def test_a_real_tool_run_through_the_registry_opens_and_then_completes_the_card(sent):
    from sdk.agent.tools import ToolRegistry

    result = await ToolRegistry.run_tool("get_system_info", {})
    await settle(0.3)
    assert result.get("success") is not False
    statuses = [e["status"] for e in sent if e["task_id"] == "active_tool"]
    assert statuses[0] == "in_progress" and statuses[-1] == "complete"

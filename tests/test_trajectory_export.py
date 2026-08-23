"""
Trajectory export.

Both halves of a trajectory were already recorded and neither could be got out.
conversation_turns holds role, content and a tool_calls column with real calls
and their results; task_event_store holds task_init, phase_start/complete,
state_transition and gate_failed. Nothing assembled either into a run.

This matters for the voice adapter. It trains on generated stories, which is
defensible for voice and a poor substitute for behaviour -- when to reach for a
tool, which one, and how to recover from a failed gate are things only real runs
can teach.
"""
import json
from pathlib import Path

import pytest

from python.harness.server import _HANDLERS
from sdk.agent.tools import ToolRegistry
from sdk.agent.trajectory import (
    DEFAULT_LIMIT,
    _decode,
    _normalise_tool_calls,
    summarise,
    write_jsonl,
)

MODULE = Path("sdk/agent/trajectory.py")


# --- reachable by both parties ----------------------------------------------


def test_export_is_dispatchable():
    assert "trajectory.export" in _HANDLERS


def test_export_is_a_registered_tool():
    assert "export_trajectories" in {t["name"] for t in ToolRegistry.get_tools()}


# --- double-encoded columns -------------------------------------------------


def test_decode_handles_json_inside_a_json_string():
    """A jsonb codec is registered per connection while callers pass
    json.dumps(...) strings, so stored values are JSON strings containing JSON.
    Decoding once leaves a str, and iterating it yields characters."""
    once = json.dumps({"goal": "x"})
    twice = json.dumps(once)

    assert _decode(twice) == {"goal": "x"}
    assert _decode(once) == {"goal": "x"}
    assert _decode({"goal": "x"}) == {"goal": "x"}


def test_decode_returns_plain_text_unchanged():
    assert _decode("not json at all") == "not json at all"


# --- tool calls -------------------------------------------------------------


def test_tool_calls_parse_into_name_args_result():
    raw = json.dumps(json.dumps([
        {"name": "web_search", "args": {"query": "x"}, "result": "ok"},
    ]))

    calls = _normalise_tool_calls(raw)

    assert calls == [{"name": "web_search", "args": {"query": "x"}, "result": "ok"}]


def test_a_long_result_is_truncated():
    """A crawl result can be tens of kilobytes and would dominate the export
    without adding signal about the decision."""
    raw = json.dumps([{"name": "crawl", "args": {}, "result": "x" * 9000}])

    calls = _normalise_tool_calls(raw)

    assert len(calls[0]["result"]) <= 2000


@pytest.mark.parametrize("raw", [None, "null", "[]", "nonsense", {"not": "a list"}])
def test_unusable_tool_calls_yield_nothing(raw):
    assert _normalise_tool_calls(raw) == []


# --- privacy ----------------------------------------------------------------


def test_content_is_excluded_by_default_at_both_entry_points():
    """conversation_turns contains everything ever said to her. The tool and the
    RPC both default to structure-plus-tool-calls without the prose."""
    tool_source = Path("sdk/agent/agent_authoring.py").read_text(encoding="utf-8")
    server_source = Path("python/harness/server.py").read_text(encoding="utf-8")

    assert "include_content: bool = False" in tool_source
    assert 'params.get("include_content", False)' in server_source


def test_excluding_content_still_reports_its_size():
    """Structure without prose has to remain interpretable."""
    source = MODULE.read_text(encoding="utf-8")

    assert '"content_chars"' in source


def test_export_is_always_bounded():
    """An export is a deliberate act with a scope, not a table dump."""
    source = MODULE.read_text(encoding="utf-8")

    assert "LIMIT $" in source
    assert isinstance(DEFAULT_LIMIT, int) and DEFAULT_LIMIT > 0


# --- the two streams are not joined -----------------------------------------


def test_the_streams_stay_separate():
    """A chat session and an autonomous task share no key in the schema.
    Stitching them on a guess would invent history."""
    source = MODULE.read_text(encoding="utf-8")

    assert '"kind": "conversation"' in source
    assert '"kind": "task"' in source
    # No attempt to correlate session_id with a task.
    assert "session_id = tasks" not in source
    assert "JOIN tasks" not in source


# --- summary ----------------------------------------------------------------


def test_the_summary_counts_tools_and_failures():
    """The tool histogram is the point: which tools were reached for in real
    runs, answered from behaviour rather than from invented prompts."""
    trajectories = [{
        "kind": "conversation",
        "turn_count": 2,
        "turns": [
            {"tool_calls": [{"name": "web_search", "result": "ok"}]},
            {"tool_calls": [{"name": "run_workspace_command", "result": "Tool execution failed"}]},
        ],
    }]

    summary = summarise(trajectories)

    assert summary["tool_calls"] == 2
    assert summary["tools_used"]["web_search"] == 1
    assert summary["tool_failures"]["run_workspace_command"] == 1
    assert "web_search" not in summary["tool_failures"]


def test_task_statuses_are_counted_separately():
    summary = summarise([
        {"kind": "task", "status": "complete"},
        {"kind": "task", "status": "failed"},
        {"kind": "task", "status": "complete"},
    ])

    assert summary["task_statuses"] == {"complete": 2, "failed": 1}


# --- output -----------------------------------------------------------------


def test_jsonl_is_one_record_per_line(tmp_path):
    """JSONL because training pipelines read it, and because a partial write
    stays parseable up to the last complete line."""
    out = tmp_path / "traj.jsonl"

    result = write_jsonl([{"kind": "task", "task_id": "a"}, {"kind": "task", "task_id": "b"}], out)

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert result["count"] == 2
    assert len(lines) == 2
    assert json.loads(lines[0])["task_id"] == "a"


def test_jsonl_survives_values_json_cannot_serialise(tmp_path):
    """Timestamps arrive as datetimes; an export must not die on one."""
    from datetime import datetime

    out = tmp_path / "traj.jsonl"
    write_jsonl([{"kind": "task", "at": datetime.now()}], out)

    assert json.loads(out.read_text(encoding="utf-8").strip())["kind"] == "task"

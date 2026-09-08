"""
The interactive workflow driver runs real steps, and can fail.

UI-122. ``run_workflow_node`` walks a flow asking the user each
``interactive_prompt`` question, which is a genuinely different job from
``execute_node``. But its non-interactive branches were mocks:

    elif node_type == "custom_script":
        # Mock execution or basic handling
        state["variables"][current_id] = "Executed script"

Nothing ran, and the walk then reached the output node and returned
``*** WORKFLOW COMPLETE ***``. A flow with a script step reported success
having executed nothing -- the same silent-success shape as UI-014 and UI-018.

Both drivers now share ``WorkflowExecutor.run_single_node``, so there is one
node dispatch rather than two that can drift.
"""
from __future__ import annotations

import json

import pytest

from python.harness.workflow_executor import WorkflowExecutor
from sdk.agent.tools import run_workflow_node


def _write(nodes_dir, node_id, spec):
    (nodes_dir / f"{node_id}.html").write_text(
        f'<html><body><article data-node-id="{node_id}" data-node-type="workflow" '
        f'data-execution-authority="1.00">'
        f'<section id="semantic-memory"><p>flow</p></section>'
        f'<script type="application/json" id="node-state">{json.dumps(spec)}</script>'
        f"</article></body></html>",
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """
    Pin TALA_USER_DATA rather than chdir.

    run_workflow_node used to build Path("nodes/...") from the working
    directory, which this fixture originally exploited. That is a portability
    bug -- Tala runs from a USB stick on any OS, and cwd is wherever the app
    was launched from, not where she is installed. Both now go through
    resolve_active_nodes_dir(), so the test pins the same env var the resolver
    reads.
    """
    (tmp_path / "nodes").mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    return tmp_path / "nodes"


def _flow(*nodes):
    return {"flow_id": "f", "name": "Interview", "nodes": list(nodes)}


def _node(nid, ntype, config=None, on_success=None, on_failure=None):
    conns = {}
    if on_success:
        conns["onSuccess"] = on_success
    if on_failure:
        conns["onFailure"] = on_failure
    return {"id": nid, "type": ntype, "label": nid, "config": config or {},
            "connections": conns}


@pytest.mark.asyncio
async def test_a_tool_step_actually_runs(workspace, monkeypatch):
    """The mock recorded 'Executed script' and moved on. This asserts the tool
    is genuinely invoked."""
    called = []

    async def fake_run_tool(name, args, authority_level="auto"):
        called.append((name, args))
        return {"ok": True}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    _write(workspace, "flow-x", _flow(
        _node("t", "trigger", on_success="work"),
        _node("work", "tool", {"tool": "do_it", "args": {"a": 1}}, on_success="end"),
        _node("end", "output"),
    ))

    out = await run_workflow_node(action="start", node_id="flow-x")

    assert called == [("do_it", {"a": 1})], "the step was not executed"
    assert out["success"] is True


@pytest.mark.asyncio
async def test_a_failing_step_does_not_report_completion(workspace, monkeypatch):
    """The core of UI-122: completion was reported regardless of what happened."""
    async def fake_run_tool(name, args, authority_level="auto"):
        return {"success": False, "error": "it broke"}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    _write(workspace, "flow-y", _flow(
        _node("t", "trigger", on_success="work"),
        _node("work", "tool", {"tool": "do_it", "args": {}}, on_success="end"),
        _node("end", "output"),
    ))

    out = await run_workflow_node(action="start", node_id="flow-y")

    assert out["success"] is False
    assert "it broke" in out["error"]


@pytest.mark.asyncio
async def test_an_unknown_step_type_fails_instead_of_passing_through(workspace):
    """The generic 'Executed {node_type}' pass-through is gone."""
    _write(workspace, "flow-z", _flow(
        _node("t", "trigger", on_success="work"),
        _node("work", "not_a_real_type", {}, on_success="end"),
        _node("end", "output"),
    ))

    out = await run_workflow_node(action="start", node_id="flow-z")

    assert out["success"] is False
    assert "not_a_real_type" in out["error"]


@pytest.mark.asyncio
async def test_a_failed_step_follows_its_failure_branch(workspace, monkeypatch):
    async def fake_run_tool(name, args, authority_level="auto"):
        return {"success": False, "error": "nope"}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    _write(workspace, "flow-r", _flow(
        _node("t", "trigger", on_success="work"),
        _node("work", "tool", {"tool": "x", "args": {}},
              on_success="end", on_failure="recover"),
        _node("recover", "output"),
        _node("end", "output"),
    ))

    out = await run_workflow_node(action="start", node_id="flow-r")
    # The failure handler runs, but the original failure remains visible.
    assert out["success"] is False
    assert "recover" in out["execution"]["execution_path"]


def test_there_is_one_node_dispatch_not_two():
    """Two copies would drift, and the drifted copy would be the silent one."""
    assert hasattr(WorkflowExecutor, "run_single_node")

    from pathlib import Path
    source = Path("sdk/agent/tools.py").read_text(encoding="utf-8")
    block = source.split("async def run_workflow_node", 1)[1].split(
        "@register_tool", 1)[0]

    assert "run_single_node" in block, "the interactive driver reimplements dispatch"

    # Check for the mock *assignments*, not the strings anywhere -- the comment
    # explaining what was removed necessarily quotes them.
    code = "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )
    assert '= "Executed script"' not in code
    assert 'f"Executed {node_type}"' not in code

"""
Acting on an HTML-NN, and passing one step's output into the next.

Steven, Q-34:

    "A workflow is a series of tool calls, that pass one bit of info from one
    tool to the next until desired outcome. Tala has the ability to call an
    html-nn and act upon that. Or at least she should."

Three gaps sat between that sentence and the code:

* no verb that runs a node (UI-106) -- read, list and update existed, act did
  not, so a compiled workflow node documented how to execute itself to a reader
  with no mechanism to follow it (UI-060);
* no ``tool`` node type, so a workflow could not call a registered tool at all,
  and an unknown node type reported SUCCESS while doing nothing;
* no interpolation, so nothing could be passed from one step to the next --
  every step ran on whatever was hardcoded when the flow was saved.
"""
from __future__ import annotations

import json

import pytest

from python.harness.workflow_executor import SUPPORTED_NODE_TYPES, WorkflowExecutor
from sdk.agent import node_execution as ne


# -- interpolation ------------------------------------------------------------

def _executor(workflow=None):
    return WorkflowExecutor(workflow or {"nodes": []})


def test_a_whole_reference_keeps_its_type():
    """So a dict or list survives being handed to the next step."""
    ex = _executor()
    ex.variables = {"step_a": {"result": {"rows": [1, 2, 3]}}}
    assert ex._resolve("{{step_a.result.rows}}") == [1, 2, 3]


def test_an_embedded_reference_is_stringified():
    ex = _executor()
    ex.variables = {"name": "Weavor"}
    assert ex._resolve("Hello {{name}}, welcome") == "Hello Weavor, welcome"


def test_references_resolve_inside_nested_config():
    ex = _executor()
    ex.variables = {"q": "tala"}
    resolved = ex._resolve({"args": {"query": "{{q}}", "opts": ["{{q}}", "static"]}})
    assert resolved == {"args": {"query": "tala", "opts": ["tala", "static"]}}


def test_an_unresolved_reference_is_reported_not_silently_blanked():
    """A misspelled reference must not become an empty argument that the tool
    then 'succeeds' on."""
    ex = _executor()
    ex.variables = {"real": 1}
    missing: list[str] = []
    out = ex._resolve("{{typo}}", missing)
    assert missing == ["typo"]
    assert out == "{{typo}}"


def test_a_step_that_produced_none_is_not_confused_with_a_missing_one():
    ex = _executor()
    ex.variables = {"step_a": {"result": None}}
    missing: list[str] = []
    assert ex._resolve("{{step_a.result}}", missing) is None
    assert missing == []


# -- the tool node type -------------------------------------------------------

def _flow(*nodes, flow_id="flow-test", name="Test"):
    return {"flow_id": flow_id, "name": name, "nodes": list(nodes)}


def _node(nid, ntype, config=None, on_success=None):
    return {
        "id": nid, "type": ntype, "label": nid,
        "config": config or {},
        "connections": {"onSuccess": on_success} if on_success else {},
    }


@pytest.mark.asyncio
async def test_a_tool_node_calls_the_registered_tool(monkeypatch):
    calls = []

    async def fake_run_tool(name, args, authority_level="auto"):
        calls.append((name, args))
        return {"ok": True, "echo": args}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    flow = _flow(
        _node("t", "trigger", on_success="call"),
        _node("call", "tool", {"tool": "web_search", "args": {"query": "tala"}}),
    )
    result = await WorkflowExecutor(flow).execute()

    assert calls == [("web_search", {"query": "tala"})]
    assert result["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_one_tool_receives_the_previous_tool_s_output(monkeypatch):
    """This is Steven's definition of a workflow, tested directly."""
    seen = []

    async def fake_run_tool(name, args, authority_level="auto"):
        seen.append((name, args))
        if name == "first":
            return {"value": "carried"}
        return {"ok": True}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    flow = _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"tool": "first", "args": {}}, on_success="b"),
        _node("b", "tool", {"tool": "second",
                            "args": {"passed": "{{step_a.result.result.value}}"}}),
    )
    result = await WorkflowExecutor(flow).execute()

    assert result["status"] == "COMPLETED"
    assert seen[1] == ("second", {"passed": "carried"})


@pytest.mark.asyncio
async def test_a_tool_node_with_a_broken_reference_refuses_to_call(monkeypatch):
    called = []

    async def fake_run_tool(name, args, authority_level="auto"):
        called.append(name)
        return {}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    flow = _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"tool": "thing", "args": {"x": "{{nope.missing}}"}}),
    )
    result = await WorkflowExecutor(flow).execute()

    assert not called, "a tool was called with an unresolved placeholder"
    assert result["status"] == "FAILED"


@pytest.mark.asyncio
async def test_a_failing_tool_fails_the_step(monkeypatch):
    """run_tool reports failure in-band; a dict saying success=False is a
    failed step, not a successful one containing the word."""
    async def fake_run_tool(name, args, authority_level="auto"):
        return {"success": False, "error": "Tool 'x' not found in registry."}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    flow = _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"tool": "x", "args": {}}),
    )
    result = await WorkflowExecutor(flow).execute()
    assert result["status"] == "FAILED"


@pytest.mark.asyncio
async def test_a_tool_node_with_no_tool_named_fails():
    flow = _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"args": {}}),
    )
    result = await WorkflowExecutor(flow).execute()
    assert result["status"] == "FAILED"


# -- unknown node types -------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unknown_node_type_fails_rather_than_reporting_success():
    """It used to log 'Custom action node type X logic executed', return
    {'status': 'ok'} and finish COMPLETED, having done nothing at all."""
    flow = _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "does_not_exist", {}),
    )
    result = await WorkflowExecutor(flow).execute()

    assert result["status"] == "FAILED"
    step = result["variables"]["step_a"]
    assert step["success"] is False
    assert "does_not_exist" in step["message"]
    # and it says what would have worked
    assert "tool" in step["message"]


def test_the_supported_types_include_tool():
    assert "tool" in SUPPORTED_NODE_TYPES
    assert "trigger" in SUPPORTED_NODE_TYPES


# -- execute_node -------------------------------------------------------------

def _write_flow_node(directory, node_id, spec, authority="1.00", node_type="workflow"):
    path = directory / f"{node_id}.html"
    path.write_text(
        f'<html><body><article data-node-id="{node_id}" '
        f'data-node-type="{node_type}" data-execution-authority="{authority}">'
        f'<section id="semantic-memory"><p>A workflow.</p></section>'
        f'<script type="application/json" id="node-state">{json.dumps(spec)}</script>'
        f"</article></body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def nodes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ne, "_nodes_dir", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_execute_node_runs_the_flow_from_the_node(nodes_dir, monkeypatch):
    """The document that was retrieved must be the document that runs --
    workflow.execute reads a JSON sidecar, which made the node decorative."""
    calls = []

    async def fake_run_tool(name, args, authority_level="auto"):
        calls.append(name)
        return {"ok": True}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    _write_flow_node(nodes_dir, "flow-abc", _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"tool": "web_search", "args": {"query": "x"}}),
    ))

    result = await ne.execute_node_by_id("flow-abc")
    assert result["success"] is True
    assert result["executed_from"] == "html-nn"
    assert calls == ["web_search"]


@pytest.mark.asyncio
async def test_the_flow_prefix_is_optional(nodes_dir):
    _write_flow_node(nodes_dir, "flow-abc", _flow(_node("t", "trigger")))
    result = await ne.execute_node_by_id("abc")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_a_documentation_node_cannot_be_executed(nodes_dir):
    """Every tool-catalogue and program-doc node is a reference work. Running
    one would turn a document into a program."""
    _write_flow_node(nodes_dir, "tool-web-search", _flow(_node("t", "trigger")),
                     authority="0.0", node_type="function-doc")
    result = await ne.execute_node_by_id("tool-web-search")

    assert result["success"] is False
    assert "function-doc" in result["error"]


@pytest.mark.asyncio
async def test_a_workflow_without_authority_is_refused(nodes_dir):
    _write_flow_node(nodes_dir, "flow-weak", _flow(_node("t", "trigger")),
                     authority="0.10")
    result = await ne.execute_node_by_id("flow-weak")

    assert result["success"] is False
    assert "execution authority" in result["error"]


@pytest.mark.asyncio
async def test_a_missing_node_says_so(nodes_dir):
    result = await ne.execute_node_by_id("flow-nope")
    assert result["success"] is False
    assert "list_neural_nodes" in result["error"]


@pytest.mark.asyncio
async def test_a_node_with_no_workflow_in_it_is_not_run(nodes_dir):
    _write_flow_node(nodes_dir, "flow-empty", {"name": "Empty"})
    result = await ne.execute_node_by_id("flow-empty")

    assert result["success"] is False
    assert "no runnable workflow" in result["error"]


@pytest.mark.asyncio
async def test_a_failed_workflow_is_not_reported_as_a_successful_call(nodes_dir):
    """The 'status: failed read as success' pattern, closed explicitly."""
    _write_flow_node(nodes_dir, "flow-bad", _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "does_not_exist", {}),
    ))
    result = await ne.execute_node_by_id("flow-bad")

    assert result["success"] is False
    assert result["status"] == "FAILED"


@pytest.mark.asyncio
async def test_inputs_reach_the_first_step(nodes_dir, monkeypatch):
    seen = {}

    async def fake_run_tool(name, args, authority_level="auto"):
        seen.update(args)
        return {"ok": True}

    from sdk.agent.tools import ToolRegistry
    monkeypatch.setattr(ToolRegistry, "run_tool", fake_run_tool)

    _write_flow_node(nodes_dir, "flow-in", _flow(
        _node("t", "trigger", on_success="a"),
        _node("a", "tool", {"tool": "echo", "args": {"q": "{{topic}}"}}),
    ))
    result = await ne.execute_node_by_id("flow-in", {"topic": "starlancer"})

    assert result["success"] is True
    assert seen == {"q": "starlancer"}


@pytest.mark.asyncio
async def test_list_executable_nodes_excludes_documents(nodes_dir):
    _write_flow_node(nodes_dir, "flow-real", _flow(_node("t", "trigger")))
    _write_flow_node(nodes_dir, "tool-something", _flow(_node("t", "trigger")),
                     authority="0.0", node_type="function-doc")

    listing = await ne.list_executable_nodes()
    ids = [n["node_id"] for n in listing["nodes"]]

    assert "flow-real" in ids
    assert "tool-something" not in ids

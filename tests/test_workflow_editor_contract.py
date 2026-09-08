"""
The contract between the workflow editor, Tala and the two executors.

The editor is generated from ``flow_schema`` and both engines dispatch on it,
so the thing worth testing is not any one of them but that they still agree: a
block the palette offers must be a block something runs, a workflow Tala writes
must open in the editor, and a workflow saved from the editor must execute as
drawn.

These are the checks that would have caught the faults this work fixed —
``interactive_prompt`` in the UI with no executor branch, ``node``/``bash``/
``mcp_tool`` runnable with no way to reach them, and ``custom_script``
reporting success while executing nothing.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from sdk.agent import flow_schema
from python.harness import workflow_executor
from python.harness.workflow_executor import WorkflowExecutor


# ---------------------------------------------------------------------------
# Registry ↔ executor parity
# ---------------------------------------------------------------------------


def _executor_dispatch_types() -> set[str]:
    """The node types ``run_single_node`` actually branches on."""
    import inspect

    source = inspect.getsource(WorkflowExecutor.run_single_node)
    return set(re.findall(r'node_type == "([a-z_]+)"', source))


def test_every_offered_block_is_a_block_something_runs():
    """
    The palette cannot offer what nothing executes.

    The five hand-written palette entries covered five of nine implemented
    types, and one entry (`interactive_prompt`) had no executor branch at all,
    so dragging it out produced a step that failed as an unknown type.
    """
    assert flow_schema.FLOW_TYPES == _executor_dispatch_types()


def test_every_implemented_type_is_offered():
    """The other direction: `node`, `bash` and `mcp_tool` ran and were unreachable."""
    for node_type in _executor_dispatch_types():
        assert node_type in flow_schema.NODE_TYPES, f"{node_type} runs but is not in the palette"


def test_the_refusal_message_lists_the_real_types():
    """SUPPORTED_NODE_TYPES omitted `node`, a type with a working branch."""
    assert set(workflow_executor.SUPPORTED_NODE_TYPES) == flow_schema.FLOW_TYPES
    assert "node" in workflow_executor.SUPPORTED_NODE_TYPES


def test_every_type_belongs_to_a_family_the_palette_groups():
    for spec in flow_schema.NODE_TYPES.values():
        assert spec.family in flow_schema.FAMILIES
        assert spec.engine in flow_schema.ENGINES


def test_a_declared_default_is_a_declared_field():
    """A default for a field that does not exist would never reach a config."""
    for spec in flow_schema.NODE_TYPES.values():
        names = {f.name for f in spec.fields}
        assert set(spec.as_dict()["defaults"]) <= names


# ---------------------------------------------------------------------------
# One representation
# ---------------------------------------------------------------------------


DAG_SPEC = {
    "workflow": {
        "flow_id": "self-extension",
        "consumer": "sdk.agent.graph_engine.DAGExecutionEngine._load_workflow_spec",
        "steps": [
            {"step": "research", "node_type": "research", "depends_on": [],
             "description": "Research {goal}", "success_criteria": {"min_sources": 1},
             "max_retries": 3},
            {"step": "synthesis", "node_type": "synthesis", "depends_on": ["research"],
             "description": "Design", "success_criteria": {}, "max_retries": 3},
        ],
        "on_failure": {"rollback_written_files": True},
    }
}


def test_a_goal_engine_chain_opens_as_a_flow():
    """
    The chain Tala runs autonomously and the flow the user draws were two
    documents that could not read each other.
    """
    flow = flow_schema.normalize(DAG_SPEC)
    assert flow["engine"] == "dag"
    assert [n["id"] for n in flow["nodes"]] == ["research", "synthesis"]
    # It has cables to draw, derived from the dependencies.
    assert flow["nodes"][0]["connections"]["onSuccess"] == "synthesis"
    # And positions, so it lands somewhere on the canvas rather than at 0,0.
    assert all(n["position"]["x"] > 0 for n in flow["nodes"])


def test_the_chain_survives_the_round_trip():
    """Opening it in the editor and saving must not change what runs."""
    flow = flow_schema.normalize(DAG_SPEC)
    spec = flow_schema.to_dag_spec(flow)
    assert spec["steps"] == DAG_SPEC["workflow"]["steps"]
    assert spec["consumer"] == DAG_SPEC["workflow"]["consumer"]
    assert spec["on_failure"] == DAG_SPEC["workflow"]["on_failure"]


def test_rearranging_on_the_canvas_reorders_the_chain():
    """
    depends_on is what the DAG engine runs on, and the canvas edits
    connections. A flow rearranged in the editor must execute as drawn.
    """
    flow = flow_schema.normalize(DAG_SPEC)
    for node in flow["nodes"]:
        node["depends_on"] = []
    flow["nodes"][0]["connections"] = {}
    flow["nodes"][1]["connections"] = {"onSuccess": "research"}

    spec = flow_schema.to_dag_spec(flow)
    by_id = {s["step"]: s for s in spec["steps"]}
    assert by_id["research"]["depends_on"] == ["synthesis"]
    assert by_id["synthesis"]["depends_on"] == []


def test_normalising_twice_changes_nothing():
    flow = flow_schema.normalize(DAG_SPEC)
    assert flow_schema.normalize(flow) == flow


def test_a_ten_document_opens_as_an_eleven_document():
    legacy = {
        "schema": "tala-flow/1.0", "flow_id": "old", "name": "Old",
        "nodes": [{
            "id": "s", "type": "trigger", "label": "Start",
            "config": {"source_node_id": "default", "description": "kick off"},
            "connections": {"onSuccess": None, "onFailure": None},
            "position": {"x": 10, "y": 20},
        }],
    }
    flow = flow_schema.normalize(legacy)
    assert flow["schema"] == flow_schema.SCHEMA
    # 1.0 kept a step's prose inside config; 1.1 gives it its own field.
    assert flow["nodes"][0]["description"] == "kick off"
    assert "description" not in flow["nodes"][0]["config"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _flow(*nodes, **extra) -> dict:
    return {"schema": "tala-flow/1.1", "flow_id": "f", "name": "F",
            "nodes": list(nodes), **extra}


def _node(node_id, node_type, config=None, connections=None) -> dict:
    return {"id": node_id, "type": node_type, "label": node_id,
            "config": config or {}, "connections": connections or {}}


def _codes(flow, **kwargs) -> set[str]:
    return {f.code for f in flow_schema.validate(flow, **kwargs)}


def test_a_dangling_connection_is_an_error():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "gone"}),
        _node("out", "output"),
    )
    assert "dangling_link" in _codes(flow)


def test_a_missing_required_argument_is_an_error():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "t"}),
        _node("t", "tool", config={"args": {}}, connections={"onSuccess": "out"}),
        _node("out", "output"),
    )
    assert "missing_config" in _codes(flow)


def test_an_unregistered_tool_is_an_error_when_the_registry_is_known():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "t"}),
        _node("t", "tool", config={"tool": "no_such_tool"}, connections={"onSuccess": "out"}),
        _node("out", "output"),
    )
    assert "unknown_tool" in _codes(flow, known_tools={"read_workspace_file"})
    # Not guessed at when the caller cannot supply the registry.
    assert "unknown_tool" not in _codes(flow)


def test_a_reference_to_a_step_that_does_not_exist_is_an_error():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "t"}),
        _node("t", "tool", config={"tool": "x", "args": {"p": "{{step_ghost.result}}"}},
              connections={"onSuccess": "out"}),
        _node("out", "output"),
    )
    assert "unknown_reference" in _codes(flow)


def test_a_decision_branch_nobody_connected_is_an_error():
    """The executor stops on one. This says so while the flow is being drawn."""
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "d"}),
        _node("d", "decision", config={"node_id": "decide-x", "branches": ["yes", "no"]},
              connections={"yes": "out"}),
        _node("out", "output"),
    )
    assert "unconnected_branch" in _codes(flow)


def test_a_decision_with_no_branches_is_an_error():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "d"}),
        _node("d", "decision", config={"node_id": "decide-x"}, connections={}),
    )
    assert "no_branches" in _codes(flow)


def test_the_two_engines_cannot_be_mixed():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "r"}),
        _node("r", "research"),
    )
    assert "mixed_engines" in _codes(flow)


def test_an_unreachable_step_is_a_warning_not_a_refusal():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "out"}),
        _node("out", "output"),
        _node("orphan", "auth"),
    )
    findings = flow_schema.validate(flow)
    assert "unreachable" in {f.code for f in findings}
    assert not flow_schema.errors(findings)


def test_a_flow_that_is_fine_reports_nothing():
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "out"}),
        _node("out", "output", config={"format": "json"}),
    )
    assert flow_schema.validate(flow) == []


# ---------------------------------------------------------------------------
# The steps really run
# ---------------------------------------------------------------------------


def _run(flow, inputs=None):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        WorkflowExecutor(flow).execute(inputs or {})
    )


@pytest.fixture
def no_distillation(monkeypatch):
    """
    A successful run distils itself into a skill node, which writes into the
    live nodes directory. That is not what these tests are measuring, and it
    is how a test run leaks documents into the user's own wiki.
    """
    async def _skip(self, execution_path):
        return None

    monkeypatch.setattr(WorkflowExecutor, "_distill_workflow_skill", _skip)


def test_a_script_step_actually_executes(no_distillation):
    """
    It used to eval() only pure arithmetic and, for anything else, return the
    string "Executed custom script logic." with success=True.
    """
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "calc"}),
        _node("calc", "custom_script",
              config={"code": "result = sum(range(10)) + variables['seed']"},
              connections={"onSuccess": "out"}),
        _node("out", "output", config={"value": "{{step_calc.result.script_result}}"}),
    )
    result = _run(flow, {"seed": 5})
    assert result["status"] == "COMPLETED"
    assert result["variables"]["step_calc"]["result"]["script_result"] == 50
    assert result["variables"]["step_out"]["result"]["value"] == 50


def test_a_failing_script_fails_the_step(no_distillation):
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "boom"}),
        _node("boom", "custom_script", config={"code": "raise ValueError('no')"}),
    )
    result = _run(flow)
    assert result["status"] == "FAILED"
    assert "ValueError" in result["variables"]["step_boom"]["message"]


def test_a_script_that_never_finishes_is_killed(no_distillation):
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "hang"}),
        _node("hang", "custom_script",
              config={"code": "import time; time.sleep(30)", "timeout": 2}),
    )
    result = _run(flow)
    assert result["status"] == "FAILED"
    assert "did not finish" in result["variables"]["step_hang"]["message"]


def test_a_run_result_can_be_serialised(no_distillation):
    """
    The trigger and output steps stored the variables dict inside itself, so
    every run's own reply died on "Circular reference detected".
    """
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "out"}),
        _node("out", "output"),
    )
    assert json.dumps(_run(flow, {"a": 1}), default=str)


def test_an_unanswered_question_fails_rather_than_defaulting(no_distillation):
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "ask"}),
        _node("ask", "interactive_prompt", config={"message": "Which branch?"}),
    )
    result = _run(flow)
    assert result["status"] == "FAILED"
    assert "no answer was supplied" in result["variables"]["step_ask"]["message"]


def test_a_question_answered_from_the_inputs_carries_on(no_distillation):
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "ask"}),
        _node("ask", "interactive_prompt",
              config={"message": "Which branch?", "input_key": "branch"},
              connections={"onSuccess": "out"}),
        _node("out", "output", config={"value": "{{step_ask.result.answer}}"}),
    )
    result = _run(flow, {"branch": "main"})
    assert result["status"] == "COMPLETED"
    assert result["variables"]["step_out"]["result"]["value"] == "main"


def test_a_failed_verification_fails_the_run(no_distillation):
    """"The last step ran" was the only definition of success a flow had."""
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "calc"}),
        _node("calc", "custom_script", config={"code": "result = False"},
              connections={"onSuccess": "out"}),
        _node("out", "output", config={"verify": "{{step_calc.result.script_result}}"}),
    )
    result = _run(flow)
    assert result["status"] == "FAILED"
    assert "Verification failed" in result["variables"]["step_out"]["message"]


def test_an_unknown_type_says_what_is_available(no_distillation):
    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "odd"}),
        _node("odd", "teleport"),
    )
    result = _run(flow)
    message = result["variables"]["step_odd"]["message"]
    assert result["status"] == "FAILED"
    assert "teleport" in message and "node" in message


def test_a_state_step_reads_and_writes(no_distillation, monkeypatch, tmp_path):
    """
    State nodes were reachable as tools and had no way into a flow. Pinning
    TALA_USER_DATA as well as root_dir: the node write path resolves the data
    directory itself, so root_dir alone leaks into the real wiki.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    (tmp_path / "nodes").mkdir(parents=True, exist_ok=True)

    write = _flow(
        _node("start", "trigger", connections={"onSuccess": "w"}),
        _node("w", "state",
              config={"node_id": "state-test", "mode": "write", "values": {"seen": 3}},
              connections={"onSuccess": "out"}),
        _node("out", "output"),
    )
    assert _run(write)["status"] == "COMPLETED"

    read = _flow(
        _node("start", "trigger", connections={"onSuccess": "r"}),
        _node("r", "state", config={"node_id": "state-test", "mode": "read"},
              connections={"onSuccess": "out"}),
        _node("out", "output", config={"value": "{{step_r.result.values.seen}}"}),
    )
    result = _run(read)
    assert result["status"] == "COMPLETED"
    assert result["variables"]["step_out"]["result"]["value"] == 3


def test_a_decision_routes_down_its_branch(no_distillation, monkeypatch):
    """
    Branch routing existed in the executor and nothing could reach it: the
    properties panel offered exactly onSuccess and onFailure.
    """
    async def fake_decision(node_id, inputs):
        return {"success": True, "branch": "cold", "node": node_id}

    monkeypatch.setattr(
        "sdk.agent.node_execution.execute_node_by_id", fake_decision, raising=True)

    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "d"}),
        _node("d", "decision",
              config={"node_id": "decide-temp", "branches": ["hot", "cold"]},
              connections={"hot": "hot-out", "cold": "cold-out"}),
        _node("hot-out", "output", config={"value": "hot"}),
        _node("cold-out", "output", config={"value": "cold"}),
    )
    result = _run(flow)
    assert result["execution_path"] == ["start", "d", "cold-out"]


def test_a_decision_that_answers_off_its_declared_list_stops(no_distillation, monkeypatch):
    async def fake_decision(node_id, inputs):
        return {"success": True, "branch": "lukewarm"}

    monkeypatch.setattr(
        "sdk.agent.node_execution.execute_node_by_id", fake_decision, raising=True)

    flow = _flow(
        _node("start", "trigger", connections={"onSuccess": "d"}),
        _node("d", "decision",
              config={"node_id": "decide-temp", "branches": ["hot", "cold"]},
              connections={"hot": "out", "cold": "out"}),
        _node("out", "output"),
    )
    result = _run(flow)
    assert result["status"] == "FAILED"
    assert "does not declare" in result["variables"]["step_d"]["message"]

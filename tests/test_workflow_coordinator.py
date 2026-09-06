"""
The coordinator: a workflow that routes itself through a network of nodes.

The primitives composed, but nothing composed them. Running Steven's server
example meant calling each node by hand and following the decision's `next`
manually, which is a script with extra steps -- not:

    "A workflow emerges from the movement of context and activation through
    the node network rather than requiring one monolithic program."

Two additions make it emerge. A `node` step runs another HTML-NN by id, and
a decision's branch names the connection to follow. The shape of the flow
lives in the workflow; the choice lives in the decision node; neither knows
the other's business.

The gate travels with the node, not the caller: execute_node_by_id applies
the target's own type, authority and admission checks, so coordinating a
node cannot grant it permissions it does not have alone. A workflow is not
a way to launder an unadmitted function.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from python.harness import db
from python.harness.workflow_executor import WorkflowExecutor
from sdk.agent import node_admission


def write_node(nodes_dir, node_id, node_type, state, authority=0.50):
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="tala-schema" content="neural-node/v2">
<title>Neural Node: {node_id}</title></head><body>
<article data-node-id="{node_id}" data-node-type="{node_type}"
  data-schema-version="neural-node/v2"
  data-execution-authority="{authority:.2f}" data-memory-authority="0.00"
  data-weight-relevance="1.00" data-weight-confidence="1.00">
  <header id="node-identity"><h1 id="node-title">{node_id}</h1></header>
  <section id="semantic-memory"><p>Test node.</p></section>
  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state)}
  </script>
</article></body></html>"""
    nodes_dir.mkdir(parents=True, exist_ok=True)
    path = nodes_dir / f"{node_id}.html"
    path.write_text(html, encoding="utf-8")
    return path


@pytest_asyncio.fixture
async def nodes(tmp_path, monkeypatch):
    directory = tmp_path / "nodes"
    directory.mkdir()
    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: directory
    )
    await db.init_pool()
    yield directory
    await db.execute("DELETE FROM node_admissions WHERE node_id LIKE 'wc-%'")
    await db.close_pool()


async def admit(path, node_id):
    await node_admission.admit(
        node_id, path.read_text(encoding="utf-8"), review="reviewed in a test", reviewed_by="test"
    )


def step(node_id, target, connections, inputs=None):
    return {
        "id": node_id,
        "type": "node",
        "config": {"node_id": target, "inputs": inputs or {}},
        "connections": connections,
    }


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Steven's example, coordinating itself
# ---------------------------------------------------------------------------


async def test_the_server_example_routes_itself(nodes):
    """
    "Check whether my server is running and restart it if it isn't."

    Five nodes and a workflow. Nothing calls them in order by hand.
    """
    for node_id, node_type, state in [
        ("wc-get-state", "service", {
            "code": "await call_tool('write_state_node', node_id='wc-state', "
                    "values={'status': 'STOPPED'})\nreturn 'STOPPED'"}),
        ("wc-check", "decision", {
            "code": "env = await call_tool('read_state_node', node_id='wc-state')\n"
                    "return env['output']['values']['status']",
            "branches": {"RUNNING": "wc-report", "STOPPED": "wc-restart"}}),
        ("wc-restart", "service", {
            "code": "await call_tool('write_state_node', node_id='wc-state', "
                    "values={'status': 'RUNNING', 'restarted': True})\nreturn 'restarted'"}),
        ("wc-verify", "function", {
            "code": "env = await call_tool('read_state_node', node_id='wc-state')\n"
                    "return env['output']['values']['status'] == 'RUNNING'"}),
        ("wc-report", "function", {
            "code": "env = await call_tool('read_state_node', node_id='wc-state')\n"
                    "return env['output']['values']"}),
    ]:
        await admit(write_node(nodes, node_id, node_type, state), node_id)

    workflow = {
        "flow_id": "server-check",
        "nodes": [
            {"id": "start", "type": "trigger", "config": {}, "connections": {"onSuccess": "get"}},
            step("get", "wc-get-state", {"onSuccess": "check"}),
            # The decision's own branch names decide where this goes.
            step("check", "wc-check", {"RUNNING": "report", "STOPPED": "restart"}),
            step("restart", "wc-restart", {"onSuccess": "verify"}),
            step("verify", "wc-verify", {"onSuccess": "report"}),
            step("report", "wc-report", {}),
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})

    assert result["status"] in ("SUCCESS", "COMPLETED"), result
    path = result.get("execution_path") or [n["id"] for n in result.get("nodes_run", [])]
    assert "restart" in str(path), f"the STOPPED branch was not taken: {path}"
    assert "verify" in str(path)


async def test_the_other_branch_is_taken_when_conditions_differ(nodes):
    """
    The same workflow, a different world. If the branch were decided by the
    flow rather than the decision node, this would take the same path.
    """
    for node_id, node_type, state in [
        ("wc-check", "decision", {
            "code": "return 'RUNNING'",
            "branches": {"RUNNING": "wc-report", "STOPPED": "wc-restart"}}),
        ("wc-restart", "service", {"code": "return 'should not run'"}),
        ("wc-report", "function", {"code": "return 'all well'"}),
    ]:
        await admit(write_node(nodes, node_id, node_type, state), node_id)

    workflow = {
        "flow_id": "server-check",
        "nodes": [
            {"id": "start", "type": "trigger", "config": {}, "connections": {"onSuccess": "check"}},
            step("check", "wc-check", {"RUNNING": "report", "STOPPED": "restart"}),
            step("restart", "wc-restart", {}),
            step("report", "wc-report", {}),
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})
    path = str(result.get("execution_path") or result.get("nodes_run"))

    assert "report" in path
    assert "restart" not in path, "the RUNNING branch still ran the restart"


# ---------------------------------------------------------------------------
# Coordination grants nothing
# ---------------------------------------------------------------------------


async def test_a_workflow_cannot_run_an_unadmitted_node(nodes):
    """
    The gate travels with the node. Otherwise a workflow would be a way to
    launder code nobody reviewed -- the coordinator asking for it would be
    all the permission it needed.
    """
    write_node(nodes, "wc-unadmitted", "function", {"code": "return 'ran anyway'"})

    workflow = {
        "flow_id": "sneaky",
        "nodes": [
            {"id": "start", "type": "trigger", "config": {}, "connections": {"onSuccess": "go"}},
            step("go", "wc-unadmitted", {}),
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})
    assert result["status"] == "FAILED"


async def test_an_undeclared_branch_stops_the_workflow(nodes):
    """
    Falling back to onSuccess would run the wrong half of a fork and report
    success -- the failure a decision exists to prevent.
    """
    node_id = "wc-check"
    await admit(write_node(nodes, node_id, "decision", {
        "code": "return 'RUNNING'",
        "branches": {"RUNNING": "wc-report", "STOPPED": "wc-restart"},
    }), node_id)
    await admit(write_node(nodes, "wc-report", "function", {"code": "return 1"}), "wc-report")

    workflow = {
        "flow_id": "misconnected",
        "nodes": [
            {"id": "start", "type": "trigger", "config": {}, "connections": {"onSuccess": "check"}},
            # The decision can answer RUNNING; the workflow only wires STOPPED.
            step("check", "wc-check", {"STOPPED": "report", "onSuccess": "report"}),
            step("report", "wc-report", {}),
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})

    assert result["status"] == "FAILED", (
        "an unwired branch fell through to onSuccess instead of stopping"
    )


async def test_a_node_step_must_name_a_node(nodes):
    workflow = {
        "flow_id": "nameless",
        "nodes": [
            {"id": "start", "type": "trigger", "config": {}, "connections": {"onSuccess": "go"}},
            {"id": "go", "type": "node", "config": {}, "connections": {}},
        ],
    }

    result = await WorkflowExecutor(workflow).execute({})
    assert result["status"] == "FAILED"

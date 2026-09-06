"""
The rest of the taxonomy: decision, service and state nodes.

Steven's design, in his words:

    Persona Nodes    influence behavior
    Memory Nodes     provide information
    State Nodes      represent current conditions
    Function Nodes   execute code
    Decision Nodes   evaluate conditions
    Workflow Nodes   coordinate multiple nodes
    Service Nodes    interact with external/internal systems

Function, decision and service all carry code, so all three sit behind the
admission gate. They are three types rather than one because what a node is
*for* governs how carefully it should be read: a service node touching the
network deserves a closer reading than a function adding two numbers, and a
reviewer can only weigh that if the node says which it is.

State is the opposite: data, not code. Not executable and not admitted,
because making state executable would turn every stored value into a
potential program -- the one thing the gate exists to prevent.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from python.harness import db
from sdk.agent import node_admission, node_execution

pytestmark = pytest.mark.asyncio


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
    await db.execute("DELETE FROM node_admissions WHERE node_id LIKE 'tx-%'")
    await db.close_pool()


async def _admit(path, node_id):
    await node_admission.admit(
        node_id, path.read_text(encoding="utf-8"), review="read in a test", reviewed_by="test"
    )


# ---------------------------------------------------------------------------
# Decision nodes
# ---------------------------------------------------------------------------


async def test_a_decision_reports_the_branch_it_chose(nodes):
    """
    Its answer is which way to go, so it is surfaced under its own key
    rather than left for a caller to infer from a return value.
    """
    path = write_node(nodes, "tx-decide", "decision", {
        "code": "return 'RUNNING' if inputs['up'] else 'STOPPED'",
        "branches": {"RUNNING": "tx-report", "STOPPED": "tx-restart"},
    })
    await _admit(path, "tx-decide")

    result = await node_execution.execute_node_by_id("tx-decide", {"up": False})

    assert result["success"] is True, result.get("error")
    assert result["branch"] == "STOPPED"
    assert result["next"] == "tx-restart"


async def test_a_branch_nobody_declared_is_an_error(nodes):
    """
    A route nobody declared is a route nobody reviewed, so it fails loudly
    rather than falling through to whatever comes next.
    """
    path = write_node(nodes, "tx-decide", "decision", {
        "code": "return 'SOMETHING_ELSE'",
        "branches": {"RUNNING": "tx-report", "STOPPED": "tx-restart"},
    })
    await _admit(path, "tx-decide")

    result = await node_execution.execute_node_by_id("tx-decide")

    assert result["success"] is False
    assert "not one of its declared branches" in result["error"]


async def test_a_decision_is_admitted_like_any_other_code(nodes):
    write_node(nodes, "tx-decide", "decision", {"code": "return 'A'", "branches": {"A": "x"}})

    result = await node_execution.execute_node_by_id("tx-decide")
    assert result["success"] is False
    assert "not admitted" in result["error"]


# ---------------------------------------------------------------------------
# Service nodes
# ---------------------------------------------------------------------------


async def test_a_service_node_runs_and_is_admitted_like_code(nodes):
    """
    Executes identically to a function; the type is a declaration of intent,
    and one a reviewer can check against the code.
    """
    path = write_node(nodes, "tx-service", "service", {"code": "return 'reached something'"})

    refused = await node_execution.execute_node_by_id("tx-service")
    assert refused["success"] is False and "not admitted" in refused["error"]

    await _admit(path, "tx-service")
    result = await node_execution.execute_node_by_id("tx-service")

    assert result["success"] is True, result.get("error")
    assert result["output"] == "reached something"
    assert result["node_type"] == "service"


async def test_the_three_code_types_all_need_admission():
    for node_type in ("function", "decision", "service"):
        assert node_type in node_execution.CODE_BEARING_TYPES
        assert node_type in node_execution.EXECUTABLE_TYPES


# ---------------------------------------------------------------------------
# State nodes
# ---------------------------------------------------------------------------


async def test_state_is_written_and_read_back(nodes):
    from sdk.agent.node_execution import read_state_node, write_state_node

    written = await write_state_node("tx-state", {"server": "STOPPED", "checked": 1})
    assert written["success"] is True

    read = await read_state_node("tx-state")
    assert read["success"] is True
    assert read["values"] == {"server": "STOPPED", "checked": 1}


async def test_writing_state_merges_rather_than_replaces(nodes):
    """Two writers recording different facts must not erase each other."""
    from sdk.agent.node_execution import read_state_node, write_state_node

    await write_state_node("tx-state", {"server": "STOPPED"})
    await write_state_node("tx-state", {"disk": "ok"})

    values = (await read_state_node("tx-state"))["values"]
    assert values == {"server": "STOPPED", "disk": "ok"}


async def test_a_state_node_is_not_executable(nodes):
    """
    Making state executable would turn every stored value into a potential
    program, which is the one thing the admission gate exists to prevent.
    """
    from sdk.agent.node_execution import write_state_node

    await write_state_node("tx-state", {"server": "RUNNING"})

    result = await node_execution.execute_node_by_id("tx-state")
    assert result["success"] is False
    assert "describes something rather than doing it" in result["error"]

    assert node_execution.STATE_TYPE not in node_execution.EXECUTABLE_TYPES
    assert node_execution.STATE_TYPE not in node_execution.CODE_BEARING_TYPES


async def test_state_cannot_be_smuggled_in_as_code(nodes):
    """
    A state node carrying a `code` key is still data. The type decides, not
    the presence of a field.
    """
    write_node(nodes, "tx-sneaky", "state", {"values": {}, "code": "return 'ran anyway'"},
               authority=1.00)

    result = await node_execution.execute_node_by_id("tx-sneaky")
    assert result["success"] is False


async def test_writing_state_refuses_to_overwrite_another_kind_of_node(nodes):
    from sdk.agent.node_execution import write_state_node

    write_node(nodes, "tx-fn", "function", {"code": "return 1"})

    result = await write_state_node("tx-fn", {"anything": True})
    assert result["success"] is False
    assert "Refusing to overwrite" in result["error"]


async def test_reading_the_wrong_kind_of_node_says_which_it_is(nodes):
    from sdk.agent.node_execution import read_state_node

    write_node(nodes, "tx-fn", "function", {"code": "return 1"})

    result = await read_state_node("tx-fn")
    assert result["success"] is False
    assert "not a state node" in result["error"]

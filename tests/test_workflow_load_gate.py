"""
Both doors into running a node, locked the same way.

`execute_node` checked the node's type and required execution authority of at
least MIN_EXECUTION_AUTHORITY. `DAGExecutionEngine._load_workflow_spec` read
state_data["workflow"] straight off the file and ran it on its content alone
-- no type check, no authority check. A node that declared itself
observational, or was not a workflow at all, executed anyway as long as it
carried the right key.

The node this loads names the function in its own `consumer` field, so the
door it pointed at was the unlocked one. And nodes/workflow-self-extension
declared 0.00 while carrying an executable DAG, which is how the gap stayed
invisible: the only workflow on disk was mislabelled in exactly the way that
made the missing check look unnecessary.

Contained today, because nothing writes a foreign node into nodes/. That
stops being true the moment nodes carry code and can be shared.
"""

from __future__ import annotations

import json

import pytest

from sdk.agent.graph_engine import DAGExecutionEngine
from sdk.agent.node_execution import EXECUTABLE_TYPES, MIN_EXECUTION_AUTHORITY


def _node(tmp_path, *, node_type="workflow", authority=0.50, with_workflow=True):
    state = {"workflow": {"flow_id": "t", "steps": [{"step": "a", "node_type": "research"}]}} if with_workflow else {}
    html = f"""<!DOCTYPE html><html><body>
<article data-node-id="workflow-self-extension" data-node-type="{node_type}"
         data-execution-authority="{authority:.2f}" data-schema-version="2.0">
  <section id="semantic-memory"><p>A test workflow.</p></section>
  <script type="application/json" id="node-state" data-cognitive-block="state">{json.dumps(state)}</script>
</article></body></html>"""
    path = tmp_path / "workflow-self-extension.html"
    path.write_text(html, encoding="utf-8")
    return path


@pytest.fixture
def engine_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.resolve_active_nodes_dir", lambda: tmp_path
    )
    return tmp_path


def _load(engine_dir):
    engine = DAGExecutionEngine.__new__(DAGExecutionEngine)
    return DAGExecutionEngine._load_workflow_spec(engine)


def test_a_proper_workflow_still_loads(engine_reading):
    _node(engine_reading)
    spec = _load(engine_reading)
    assert spec is not None
    assert spec["flow_id"] == "t"


def test_an_observational_node_does_not_execute(engine_reading):
    """
    The check execute_node made and this path did not. A node declaring
    itself below the threshold is a document, whatever it carries.
    """
    _node(engine_reading, authority=0.0)
    assert _load(engine_reading) is None


def test_a_non_workflow_node_does_not_execute(engine_reading):
    """
    Carrying a `workflow` key does not make something a workflow. Before
    this, any node type at all would run if the key was present.
    """
    _node(engine_reading, node_type="documentation")
    assert _load(engine_reading) is None


def test_the_two_doors_agree(engine_reading):
    """
    One gate, imported rather than restated. Two copies of a threshold are
    two things that can drift, and the drift is silent.
    """
    import inspect

    src = inspect.getsource(DAGExecutionEngine._load_workflow_spec)
    assert "MIN_EXECUTION_AUTHORITY" in src
    assert "EXECUTABLE_TYPES" in src
    assert "0.5" not in src, "the threshold is hardcoded here as well as imported"


def test_the_shipped_workflow_declares_what_it_is():
    """
    nodes/workflow-self-extension carried an executable DAG while declaring
    execution authority 0.00. A program marked observational is mislabelled,
    and it would now be refused by its own loader.
    """
    from pathlib import Path

    from sdk.agent.parser import parse_file

    node = Path(__file__).resolve().parents[1] / "nodes" / "workflow-self-extension.html"
    if not node.exists():
        pytest.skip("no self-extension workflow node in this install")

    parsed = parse_file(str(node))
    assert parsed.node_type in EXECUTABLE_TYPES
    assert parsed.execution_authority >= MIN_EXECUTION_AUTHORITY, (
        "the only shipped workflow declares less authority than running it requires"
    )

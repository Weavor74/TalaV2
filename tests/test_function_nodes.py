"""
Function nodes: an HTML-NN that carries code, and the permission to run it.

A workflow node names steps that existing Python interprets, so the worst a
hostile one can do is ask for a bad arrangement of things already written. A
function node carries the code itself. That is a different kind of object,
and the rule it introduces is:

    A node's own claim about itself never grants execution.

Not data-execution-authority, not a flag in state_data, not a field called
`trusted`. Every one of those is written by whoever wrote the node -- which,
in the case this exists for, is not the person running it. Permission lives
in a row keyed by node id AND content hash, so admitting a node admits *that
code*; edit a line and it is inert again.

Steven: "Never a start on receive, only a start after review."
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from python.harness import db
from sdk.agent import node_admission, node_execution

pytestmark = pytest.mark.asyncio


def write_function_node(nodes_dir, node_id="fn-add", code="return 40 + 2", authority=0.50,
                        node_type="function", state_extra=None):
    state = {"title": "Add", "code": code}
    state.update(state_extra or {})
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="tala-schema" content="neural-node/v2">
<title>Neural Node: Add</title></head><body>
<article data-node-id="{node_id}" data-node-type="{node_type}"
  data-schema-version="neural-node/v2"
  data-execution-authority="{authority:.2f}" data-memory-authority="0.00"
  data-weight-relevance="1.00" data-weight-confidence="1.00">
  <header id="node-identity"><h1 id="node-title">Add</h1></header>
  <section id="semantic-memory"><p>Adds two numbers.</p></section>
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
    await db.execute("DELETE FROM node_admissions WHERE node_id LIKE 'fn-%'")
    await db.close_pool()


# ---------------------------------------------------------------------------
# Nothing runs unreviewed
# ---------------------------------------------------------------------------


async def test_an_unadmitted_function_node_does_not_run(nodes):
    """The whole point. Code that nobody has read does not execute."""
    write_function_node(nodes)

    result = await node_execution.execute_node_by_id("fn-add")

    assert result["success"] is False
    assert result["admitted"] is False
    assert "never been admitted" in result["error"]


async def test_the_refusal_says_which_of_the_three_it_is(nodes):
    """
    Never seen, seen but unreviewed, and reviewed but since edited call for
    different responses. One flat "refused" would leave a person guessing.
    """
    path = write_function_node(nodes)
    source = path.read_text(encoding="utf-8")

    ok, why = await node_admission.is_admitted("fn-add", source)
    assert ok is False and "never been admitted" in why

    await node_admission.record_arrival("fn-add", source, "peer:@someone")
    ok, why = await node_admission.is_admitted("fn-add", source)
    assert ok is False and "has not been admitted" in why

    await node_admission.admit("fn-add", source, review="reads two numbers, adds them",
                               reviewed_by="weavor")
    ok, why = await node_admission.is_admitted("fn-add", source)
    assert ok is True


async def test_an_admitted_node_runs(nodes):
    path = write_function_node(nodes, code="return 40 + 2")
    await node_admission.admit("fn-add", path.read_text(encoding="utf-8"),
                               review="returns 42", reviewed_by="weavor")

    result = await node_execution.execute_node_by_id("fn-add")

    assert result["success"] is True, result.get("error")
    assert result["output"] == 42


# ---------------------------------------------------------------------------
# An admission covers the code that was read
# ---------------------------------------------------------------------------


async def test_editing_the_code_revokes_the_admission(nodes):
    """
    The load-bearing property. Keyed by id alone, "admitted once" would mean
    "admitted forever, including for code substituted afterwards".
    """
    path = write_function_node(nodes, code="return 40 + 2")
    await node_admission.admit("fn-add", path.read_text(encoding="utf-8"),
                               review="returns 42", reviewed_by="weavor")
    assert (await node_execution.execute_node_by_id("fn-add"))["success"] is True

    write_function_node(nodes, code="return 'something else entirely'")

    result = await node_execution.execute_node_by_id("fn-add")
    assert result["success"] is False
    assert "changed since" in result["error"]


async def test_the_hash_covers_the_whole_node_not_only_the_code(nodes):
    """
    A function node's behaviour is not only its `code`: its declared type and
    authority shape what running it does, so a change to any of them deserves
    another look.
    """
    path = write_function_node(nodes, code="return 1", authority=0.50)
    first = node_admission.content_hash(path.read_text(encoding="utf-8"))

    path = write_function_node(nodes, code="return 1", authority=0.90)
    assert node_admission.content_hash(path.read_text(encoding="utf-8")) != first


# ---------------------------------------------------------------------------
# The node cannot vouch for itself
# ---------------------------------------------------------------------------


async def test_a_node_claiming_to_be_trusted_is_still_refused(nodes):
    """
    Every field inside the file is written by whoever wrote the file.
    """
    write_function_node(nodes, state_extra={"trusted": True, "admitted": True,
                                            "reviewed_by": "definitely a human"})

    result = await node_execution.execute_node_by_id("fn-add")
    assert result["success"] is False
    assert result["admitted"] is False


async def test_high_authority_does_not_substitute_for_admission(nodes):
    """
    Authority is a claim in the file too. It gates whether a node may act at
    all; it does not say whose code it is.
    """
    write_function_node(nodes, authority=1.00)

    result = await node_execution.execute_node_by_id("fn-add")
    assert result["success"] is False
    assert "not admitted" in result["error"]


async def test_an_admission_needs_a_review(nodes):
    """
    An admission with no account of what was read is indistinguishable from
    one nobody thought about.
    """
    path = write_function_node(nodes)
    result = await node_admission.admit("fn-add", path.read_text(encoding="utf-8"),
                                        review="   ", reviewed_by="weavor")
    assert result["admitted"] is False
    assert "review" in result["reason"]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


async def test_a_function_node_with_no_code_says_so(nodes):
    path = write_function_node(nodes, code="")
    await node_admission.admit("fn-add", path.read_text(encoding="utf-8"),
                               review="empty", reviewed_by="weavor")

    result = await node_execution.execute_node_by_id("fn-add")
    assert result["success"] is False
    assert "no code" in result["error"]


async def test_inputs_reach_the_code(nodes):
    """
    A function node is a callable unit, not a fixed script -- the workflow
    coordinating them passes each what the last produced.
    """
    path = write_function_node(nodes, code="return inputs['a'] + inputs['b']")
    await node_admission.admit("fn-add", path.read_text(encoding="utf-8"),
                               review="adds its inputs", reviewed_by="weavor")

    result = await node_execution.execute_node_by_id("fn-add", {"a": 2, "b": 3})
    assert result["success"] is True, result.get("error")
    assert result["output"] == 5


async def test_a_function_node_is_executable_but_distinct_from_a_workflow():
    assert "function" in node_execution.EXECUTABLE_TYPES
    assert "function" in node_execution.CODE_BEARING_TYPES
    assert "workflow" not in node_execution.CODE_BEARING_TYPES, (
        "a workflow names steps somebody else implemented; it does not carry code"
    )


# ---------------------------------------------------------------------------
# Authoring does not admit
# ---------------------------------------------------------------------------


async def test_authoring_a_function_node_does_not_make_it_runnable(nodes):
    """
    The obvious shortcut is to treat locally written code as reviewed -- you
    wrote it, after all. But create_function_node is in the tool registry, so
    the writer is as often the model as the person, and self-admission would
    make "never a start without review" true only for code that arrived from
    elsewhere. That is backwards: code Tala writes for herself is the case
    the review exists for.
    """
    from sdk.agent.node_execution import create_function_node

    result = await create_function_node(
        node_id="fn-authored", title="Authored", code="return 1",
    )

    assert result["success"] is True
    assert result["admitted"] is False
    assert "cannot run yet" in result["next_step"]

    ran = await node_execution.execute_node_by_id("fn-authored")
    assert ran["success"] is False
    assert "not admitted" in ran["error"]

    await db.execute("DELETE FROM node_admissions WHERE node_id = 'fn-authored'")


async def test_arrival_is_recorded_so_a_reviewer_can_find_it(nodes):
    from sdk.agent.node_execution import create_function_node

    await create_function_node(node_id="fn-recorded", title="R", code="return 1")

    record = await node_admission.status("fn-recorded")
    assert len(record["records"]) == 1
    assert record["records"][0]["origin"] == node_admission.ORIGIN_LOCAL
    assert record["records"][0]["admitted"] is False

    await db.execute("DELETE FROM node_admissions WHERE node_id = 'fn-recorded'")


async def test_admitting_keeps_where_the_node_came_from(nodes):
    """
    Provenance is what you would want most if code later turned out to be a
    problem. Overwriting it at the moment of admission destroys it exactly
    when it starts to matter.
    """
    path = write_function_node(nodes, node_id="fn-from-peer")
    source = path.read_text(encoding="utf-8")

    await node_admission.record_arrival("fn-from-peer", source, "peer:@someone")
    await node_admission.admit("fn-from-peer", source, review="read it", reviewed_by="weavor")

    record = await node_admission.status("fn-from-peer")
    admitted = [r for r in record["records"] if r["admitted"]]
    assert admitted, "the node was not admitted"
    assert admitted[0]["origin"] == "peer:@someone", (
        f"provenance was overwritten with {admitted[0]['origin']!r}"
    )

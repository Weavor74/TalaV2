"""
Findings from the cloud review of the swarm topology work.

Two mattered. Both are the failure class this branch exists to remove: work that
stops happening without anything saying so.

  in_flight leak   evaluate_edges had two early returns above the try/finally
                   whose finally clause is the ONLY place a completing node is
                   retired from the in-flight count. Hitting either one leaked
                   the count permanently, so it never reached zero, _finalise_run
                   never fired, and the parent sat at in_progress until the
                   reaper marked it failed half an hour later.

  id collision     topology_node_id strips a leading "swarm-", so distinct
                   topology ids share one file. With overwrite defaulting to
                   True and no check on the existing file's contents, saving the
                   second silently destroyed the first and reported success.
"""
import inspect
import re
from pathlib import Path

import pytest

from sdk.agent.swarm_engine import SwarmEngine
from sdk.agent.topology_nodes import topology_node_id

ENGINE = Path("sdk/agent/swarm_engine.py")
RENDERER = Path("renderer/renderer.js")
SERVER = Path("python/harness/server.py")


def _evaluate_edges_source() -> str:
    return inspect.getsource(SwarmEngine.evaluate_edges)


# --- the in-flight leak -----------------------------------------------------


def test_every_early_return_sits_inside_the_try():
    """The finally clause is the only decrement path, so a return above it is a
    permanently leaked count."""
    source = _evaluate_edges_source()
    try_at = source.index("\n        try:")

    for match in re.finditer(r"^\s+return\s*$", source, re.M):
        assert match.start() > try_at, (
            "an early return sits above the try/finally and would skip the "
            "in-flight decrement, stalling the run"
        )


def test_the_decrement_is_still_the_finally_clause():
    """Guards the assumption the test above rests on. If retirement ever moves
    out of the finally, this whole test file is reasoning about the wrong thing."""
    source = _evaluate_edges_source()

    assert "finally:" in source
    finally_block = source.split("finally:", 1)[1]
    assert "_add_in_flight(parent_task_id, -1)" in finally_block


def test_variables_the_finally_reads_are_bound_before_the_try():
    """An early return leaves anything assigned inside the try unbound, so the
    finally would raise UnboundLocalError instead of retiring the node."""
    source = _evaluate_edges_source()
    head = source.split("\n        try:", 1)[0]

    for name in ("db_live", "halted", "outgoing_edges"):
        assert re.search(rf"^\s+{name}\s*[:=]", head, re.M), (
            f"{name} is read in the finally but not bound before the try"
        )


def test_finalise_run_does_not_take_a_topology():
    """It never read the parameter, and after the restructure `topology` is
    scoped inside the try -- passing it from the finally would raise on exactly
    the early-return path this fix exists to handle."""
    params = list(inspect.signature(SwarmEngine._finalise_run).parameters)

    assert "topology" not in params
    assert params == ["parent_task_id"]


def test_a_dropped_topology_is_reported_not_swallowed():
    """TopologyRegistry.reset() runs whenever the Topologies tab is opened, and
    a user-authored topology has no built-in fallback. Losing one mid-run must
    say so."""
    source = _evaluate_edges_source()

    assert "no longer registered" in source


# --- the node id collision --------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("swarm-code-repair", "code-repair"),
    ("swarm_my-audit", "my-audit"),
    ("swarm-parallel-audit", "parallel-audit"),
])
def test_the_collision_is_real_and_still_present(a, b):
    """Documents the shape rather than asserting it away: the ids genuinely do
    share a file. That is why the write path has to check."""
    assert topology_node_id(a) == topology_node_id(b)


def test_writing_over_a_different_topology_is_refused():
    source = Path("sdk/agent/topology_nodes.py").read_text(encoding="utf-8")

    assert 'data-topology-id="([^"]*)"' in source, "the previous file's id must be read back"
    assert "would destroy it" in source, "the refusal must say what is at stake"
    # Refused, not silently renamed -- the author chose the id.
    assert "raise ValueError" in source


# --- the UI nits ------------------------------------------------------------


def test_topology_fields_are_escaped_into_innerhtml():
    """name, role and description are only whitespace-trimmed server-side, so
    only ID_PATTERN-checked fields are safe raw. This is an Electron renderer."""
    renderer = RENDERER.read_text(encoding="utf-8")

    for raw in ("${t.name}", "${node.role}", "${node.description}", "${a.name}",
                "${this.personaName(node.agent_id)}"):
        assert raw not in renderer, f"{raw} is interpolated into innerHTML unescaped"


def test_topology_list_logs_its_failures_like_its_siblings():
    """With reload=true it does file I/O, HTML parsing and spec validation. A
    bare str(exc) with no traceback is the operator-facing version of a silent
    failure."""
    source = SERVER.read_text(encoding="utf-8")

    for handler in ("list", "create", "delete"):
        assert f'logger.exception("swarm.topology.{handler} error")' in source, (
            f"swarm.topology.{handler} does not log its exceptions"
        )

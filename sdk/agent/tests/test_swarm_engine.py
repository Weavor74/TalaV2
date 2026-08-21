"""
test_swarm_engine.py — unit coverage for swarm graph progression.

These tests exercise the orchestration *logic* with the DB mocked. The
correctness of the atomic SQL behind the join barrier, the in-flight counter
and the finalisation claim is proven separately against real Postgres in
tests/test_swarm_orchestration_e2e.py — a mock cannot demonstrate that two
concurrent UPDATEs serialise.
"""

from __future__ import annotations

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock

from sdk.agent.swarm_engine import SwarmEngine, ZombieReaper
from sdk.agent.topology import TopologyRegistry, SwarmTopology, TopologyNode, TopologyEdge
from sdk.agent.queue import BatchQueue, TaskStatus

@pytest.fixture
def dummy_topology():
    node1 = TopologyNode(id="node-start", role="researcher", description="Start node")
    node2 = TopologyNode(id="node-end", role="writer", description="End node")
    edge = TopologyEdge(from_node="node-start", to_node="node-end", condition="success")
    topology = SwarmTopology(
        topology_id="test-topo",
        name="Test Topology",
        nodes=[node1, node2],
        edges=[edge],
        failure_policy={}
    )
    TopologyRegistry.register(topology)
    yield topology
    # Cleanup logic if registry has unregister (not strictly required for isolated test mock)


@pytest.fixture
def fan_in_topology():
    """scout fans out to two surveyors; reconcile joins both."""
    topology = SwarmTopology(
        topology_id="test-fan-in",
        name="Fan-in Test Topology",
        nodes=[
            TopologyNode(id="scout", role="planner", description="Scope it"),
            TopologyNode(id="left", role="researcher", description="Left branch"),
            TopologyNode(id="right", role="researcher", description="Right branch"),
            TopologyNode(id="reconcile", role="reviewer", description="Join both branches"),
        ],
        edges=[
            TopologyEdge(from_node="scout", to_node="left", condition="success"),
            TopologyEdge(from_node="scout", to_node="right", condition="success"),
            TopologyEdge(from_node="left", to_node="reconcile", condition="success"),
            TopologyEdge(from_node="right", to_node="reconcile", condition="success"),
        ],
        failure_policy={},
    )
    TopologyRegistry.register(topology)
    yield topology


@pytest.mark.asyncio
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
async def test_spawn_topology(mock_enqueue, dummy_topology):
    parent_id = await SwarmEngine.spawn_topology("Test goal", "test-topo")
    assert isinstance(parent_id, str)
    assert len(parent_id) > 0
    # Should enqueue 2 times: once for parent, once for the start node
    assert mock_enqueue.call_count == 2

    # Check parent enqueue
    call_args_parent = mock_enqueue.call_args_list[0][1]
    assert call_args_parent["task_id"] == parent_id
    assert call_args_parent["payload"]["topology_id"] == "test-topo"

    # Check start node enqueue
    call_args_node = mock_enqueue.call_args_list[1][1]
    assert call_args_node["payload"]["node_id"] == "node-start"
    assert call_args_node["parent_id"] == parent_id

@pytest.mark.asyncio
@patch("python.harness.db.fetch", new_callable=AsyncMock)
@patch("sdk.agent.gated_verifier.GatedVerifier.verify", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
async def test_evaluate_edges_success(mock_enqueue, mock_verify, mock_fetch, dummy_topology):
    # Mock db.fetch for load topology
    mock_fetch.return_value = [{"payload": json.dumps({"topology_id": "test-topo"})}]

    # Mock verify to pass
    mock_verify.return_value = (True, "Good", "success")

    await SwarmEngine.evaluate_edges("parent_123", "node-start", {"output": "result"})

    # Should enqueue the next node
    assert mock_enqueue.call_count == 1
    call_args = mock_enqueue.call_args_list[0][1]
    assert call_args["payload"]["node_id"] == "node-end"
    assert call_args["parent_id"] == "parent_123"

@pytest.mark.asyncio
@patch("python.harness.db.fetch", new_callable=AsyncMock)
@patch("python.harness.db.execute", new_callable=AsyncMock)
@patch("sdk.agent.gated_verifier.GatedVerifier.verify", new_callable=AsyncMock)
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
async def test_evaluate_terminal_node(mock_transition, mock_verify, mock_execute, mock_fetch, dummy_topology):
    # Mock db.fetch for: load topology, reload parent payload in _finalise_run,
    # then the child-results query.
    mock_fetch.side_effect = [
        [{"payload": json.dumps({"topology_id": "test-topo", "goal": "Finish"})}],  # topology fetch
        [{"payload": json.dumps({"topology_id": "test-topo", "goal": "Finish"})}],  # parent payload
        [{"payload": json.dumps({"node_id": "node-start"}),
          "result": {"output": "A", "success": True},
          "status": "complete"}],  # results fetch
    ]

    with patch("sdk.agent.synthesizer.SynthesizerWorker.synthesize", new_callable=AsyncMock) as mock_synth:
        mock_synth.return_value = {"final": "done"}
        await SwarmEngine.evaluate_edges("parent_123", "node-end", {"output": "result"})

        # Verify it synthesized
        mock_synth.assert_called_once()

        # Verify the synthesis result was passed straight to transition_task.
        # Regression: evaluate_edges used to write the synthesis result via a
        # raw db.execute() call and *then* call transition_task with no
        # `result` kwarg — transition_task's own UPDATE defaults `result` to
        # {}, so that second write silently clobbered the first, and the
        # parent task's persisted result was always {}. Fixed by passing
        # final_synthesis straight into transition_task's `result` kwarg
        # instead of writing it separately beforehand.
        mock_transition.assert_called_with(
            "parent_123", TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE, result={"final": "done"}
        )


@pytest.mark.asyncio
@patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
async def test_execute_node_task_skips_git_worktree(mock_transition):
    """
    Swarm nodes must not spin up a per-node git worktree/branch — they run
    directly against the shared local workspace instead, so nodes in the
    same swarm run see each other's file writes and execution has no git
    dependency. DAGExecutionEngine only attempts its own DB-lookup + auto
    `git worktree add` (via handle_task_create_worktree) when a task has no
    worktree_path already set — this proves execute_node_task always sets
    one before handing off, short-circuiting that path.
    """
    from sdk.agent.graph_engine import DAGExecutionEngine

    captured: dict = {}

    async def fake_execute_task_as_dag(self, task, policy=None):
        captured["worktree_path"] = getattr(task, "worktree_path", None)
        return {"success": True, "output": "ok", "error": None}

    with patch.object(DAGExecutionEngine, "execute_task_as_dag", new=fake_execute_task_as_dag), \
         patch("python.harness.server.handle_task_create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch.object(SwarmEngine, "evaluate_edges", new_callable=AsyncMock):
        await SwarmEngine.execute_node_task("task-x", "parent-x", "node-x", "worker", "do a thing")

    assert captured.get("worktree_path"), "worktree_path must be pre-set so DAGExecutionEngine skips auto git-worktree creation"
    assert captured["worktree_path"] == DAGExecutionEngine().root_dir
    mock_create_wt.assert_not_called()


# ---------------------------------------------------------------------------
# Fan-out / fan-in
# ---------------------------------------------------------------------------


async def _settle() -> None:
    """Let fire-and-forget asyncio.create_task dispatches actually run."""
    import asyncio

    for _ in range(3):
        await asyncio.sleep(0)


class _FakeSwarmState:
    """In-memory stand-in for the parent task's jsonb bookkeeping columns."""

    def __init__(self) -> None:
        self.arrivals: dict[str, set[str]] = {}
        self.in_flight = 0
        self.claimed: set[str] = set()
        self.finalised = False
        self.dispatched: list[str] = []

    def install(self, stack):
        """Patch SwarmEngine's atomic helpers onto this state."""

        async def record_arrival(parent_task_id, to_node, from_node):
            self.arrivals.setdefault(to_node, set()).add(from_node)
            return set(self.arrivals[to_node])

        async def add_in_flight(parent_task_id, delta):
            self.in_flight += delta
            return self.in_flight

        async def claim_node(task_id):
            if task_id in self.claimed:
                return False
            self.claimed.add(task_id)
            return True

        async def claim_finalisation(parent_task_id):
            if self.finalised:
                return False
            self.finalised = True
            return True

        async def execute_node_task(task_id, parent_task_id, node_id, role, description,
                                    retry_feedback=None, agent_id=None):
            self.dispatched.append(node_id)

        async def reset_run_state(parent_task_id):
            self.arrivals.clear()
            self.claimed.clear()
            self.in_flight = 0
            self.finalised = False

        for name, fn in (
            ("_record_arrival", record_arrival),
            ("_add_in_flight", add_in_flight),
            ("_claim_node", claim_node),
            ("_claim_finalisation", claim_finalisation),
            ("_reset_run_state", reset_run_state),
            ("execute_node_task", execute_node_task),
        ):
            stack.enter_context(patch.object(SwarmEngine, name, new=AsyncMock(side_effect=fn)))
        stack.enter_context(patch.object(SwarmEngine, "_db_available", return_value=True))
        stack.enter_context(
            patch("sdk.agent.queue.BatchQueue.enqueue_task", new_callable=AsyncMock)
        )
        return self


@pytest.mark.asyncio
async def test_join_node_waits_for_every_upstream(fan_in_topology):
    """
    A node with two incoming edges must not run when only one of them has
    arrived. Before join semantics existed, each upstream independently
    enqueued the target — and because generate_deterministic_id gives both the
    same task id, the join node was executed twice, concurrently, against one
    row.
    """
    import contextlib

    with contextlib.ExitStack() as stack:
        state = _FakeSwarmState().install(stack)
        reconcile = fan_in_topology.node("reconcile")

        first = await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, reconcile, arrived_from="left"
        )
        await _settle()
        assert first is False, "join must not dispatch on the first arrival"
        assert state.dispatched == []
        assert state.in_flight == 0, "a node that was not dispatched must not be counted in flight"

        second = await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, reconcile, arrived_from="right"
        )
        await _settle()
        assert second is True, "join must dispatch once the last upstream arrives"
        assert state.dispatched == ["reconcile"]
        assert state.in_flight == 1


@pytest.mark.asyncio
async def test_join_is_not_double_counted_by_a_repeated_arrival(fan_in_topology):
    """
    Arrivals are a set keyed by upstream node, so the same upstream arriving
    twice (a retry re-traversing its edge) cannot satisfy a two-way join on
    its own.
    """
    import contextlib

    with contextlib.ExitStack() as stack:
        state = _FakeSwarmState().install(stack)
        reconcile = fan_in_topology.node("reconcile")

        assert await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, reconcile, arrived_from="left"
        ) is False
        assert await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, reconcile, arrived_from="left"
        ) is False
        await _settle()
        assert state.dispatched == []


@pytest.mark.asyncio
async def test_node_is_claimed_once_so_it_cannot_execute_twice(fan_in_topology):
    """The atomic claim is the guard against two dispatchers of one task id."""
    import contextlib

    with contextlib.ExitStack() as stack:
        state = _FakeSwarmState().install(stack)
        left = fan_in_topology.node("left")

        assert await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, left, arrived_from="scout"
        ) is True
        assert await SwarmEngine._dispatch_node(
            "parent-1", fan_in_topology, left, arrived_from="scout"
        ) is False
        await _settle()
        assert state.dispatched == ["left"]
        assert state.in_flight == 1


@pytest.mark.asyncio
async def test_spawn_holds_in_flight_across_the_whole_fan_out(fan_in_topology):
    """
    Every start node must be dispatched before the counter can fall to zero.
    The spawn-wide guard count is what stops a fast first branch finalising
    the run while later start nodes are still being dispatched.
    """
    import contextlib

    parallel_starts = SwarmTopology(
        topology_id="test-two-starts",
        name="Two Starts",
        nodes=[
            TopologyNode(id="alpha", role="worker", description="a"),
            TopologyNode(id="beta", role="worker", description="b"),
        ],
        edges=[],
        failure_policy={},
    )
    TopologyRegistry.register(parallel_starts)

    with contextlib.ExitStack() as stack:
        state = _FakeSwarmState().install(stack)
        stack.enter_context(patch.object(SwarmEngine, "_finalise_run", new_callable=AsyncMock))

        await SwarmEngine.spawn_topology("two-start goal", "test-two-starts")
        await _settle()

        assert sorted(state.dispatched) == ["alpha", "beta"]
        # Guard released, both nodes still in flight.
        assert state.in_flight == 2


@pytest.mark.asyncio
async def test_finalisation_is_claimed_once(dummy_topology):
    """
    Converging branches must not both synthesise. Whoever loses the claim
    returns before the (expensive, user-visible) synthesis LLM call.
    """
    import contextlib

    with contextlib.ExitStack() as stack:
        state = _FakeSwarmState().install(stack)
        stack.enter_context(patch("python.harness.db.fetch", new_callable=AsyncMock, return_value=[]))
        stack.enter_context(
            patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
        )
        synth = stack.enter_context(
            patch("sdk.agent.synthesizer.SynthesizerWorker.synthesize", new_callable=AsyncMock)
        )
        synth.return_value = {"success": True, "output": "report"}

        await SwarmEngine._finalise_run("parent-1", dummy_topology)
        await SwarmEngine._finalise_run("parent-1", dummy_topology)

        synth.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalisation_reports_failed_nodes(dummy_topology):
    """
    A branch that died must appear in the synthesis. Reading only
    status='complete' children made a failed fan-out branch vanish, so the run
    read as a clean success that was quietly missing a dimension of the work.
    """
    import contextlib

    with contextlib.ExitStack() as stack:
        _FakeSwarmState().install(stack)
        fetch = stack.enter_context(patch("python.harness.db.fetch", new_callable=AsyncMock))
        fetch.side_effect = [
            [{"payload": json.dumps({"goal": "audit it"})}],
            [
                {"payload": json.dumps({"node_id": "left"}),
                 "result": {"output": "left output", "success": True}, "status": "complete"},
                {"payload": json.dumps({"node_id": "right"}),
                 "result": {"output": "", "error": "boom", "success": False}, "status": "failed"},
            ],
        ]
        stack.enter_context(
            patch("sdk.agent.queue.BatchQueue.transition_task", new_callable=AsyncMock)
        )
        synth = stack.enter_context(
            patch("sdk.agent.synthesizer.SynthesizerWorker.synthesize", new_callable=AsyncMock)
        )
        synth.return_value = {"success": False, "output": "report"}

        await SwarmEngine._finalise_run("parent-1", dummy_topology)

        upstream = synth.await_args[0][1]
        by_node = {r["node_id"]: r for r in upstream}
        assert set(by_node) == {"left", "right"}, "the failed node must reach the synthesizer"
        assert by_node["right"]["success"] is False
        assert "boom" in by_node["right"]["output"], "the failure reason must survive into the report"


# ---------------------------------------------------------------------------
# Per-node personas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_persona_reaches_the_executor(fan_in_topology):
    """
    A persona named on a topology node has to arrive at the thing that builds
    the prompt. Setting `assigned_agent_id` alone would only decorate the task
    board — the column already existed and nothing read it during execution.
    """
    import contextlib

    fan_in_topology.node("left").agent_id = "agent-zara-9f2c"
    try:
        with contextlib.ExitStack() as stack:
            state = _FakeSwarmState().install(stack)
            captured = {}

            async def capture(task_id, parent_task_id, node_id, role, description,
                              retry_feedback=None, agent_id=None):
                state.dispatched.append(node_id)
                captured[node_id] = agent_id

            stack.enter_context(
                patch.object(SwarmEngine, "execute_node_task", new=AsyncMock(side_effect=capture))
            )
            stack.enter_context(patch("python.harness.db.execute", new_callable=AsyncMock))

            await SwarmEngine._dispatch_node(
                "parent-1", fan_in_topology, fan_in_topology.node("left"), arrived_from="scout"
            )
            await SwarmEngine._dispatch_node(
                "parent-1", fan_in_topology, fan_in_topology.node("right"), arrived_from="scout"
            )
            await _settle()

            assert captured["left"] == "agent-zara-9f2c"
            assert captured["right"] is None, "a node with no persona must stay personaless"
    finally:
        fan_in_topology.node("left").agent_id = None


@pytest.mark.asyncio
async def test_persona_prompt_never_overrides_the_output_contract():
    """
    A persona supplies character; it must not be able to talk the synthesis
    node out of emitting only code. The writing node downstream parses this
    response as source, so a chatty answer fails validation rather than
    reading as a refusal.
    """
    from sdk.agent.graph_engine import DAGExecutionEngine

    engine = DAGExecutionEngine()
    engine._persona_prompt = "You are Zara. Always explain your reasoning in prose first."

    persona = getattr(engine, "_persona_prompt", "")
    output_contract = (
        "Output ONLY valid Python code inside a single ```python ``` code block. "
        "Never write prose, thinking logs, or markdown outside the code block."
    )
    composed = (
        f"{persona}\n\nFor this task you are working as an expert Python software engineer. {output_contract}"
        if persona else f"You are an expert Python software engineer. {output_contract}"
    )

    assert composed.index("Zara") < composed.index("Output ONLY"), (
        "the output contract must come last so it is the most recent instruction"
    )
    assert output_contract in composed, "the contract must survive intact alongside a persona"


@pytest.mark.asyncio
async def test_missing_persona_is_reported_not_swallowed(caplog):
    """
    A persona deleted after the topology was authored must not make the run
    look identical to one that never named a persona.
    """
    from sdk.agent.graph_engine import DAGExecutionEngine

    engine = DAGExecutionEngine()
    with patch("python.harness.db.fetchrow", new_callable=AsyncMock, return_value=None):
        with caplog.at_level("WARNING"):
            prompt = await engine._resolve_persona_prompt("agent-gone-0000")

    assert prompt == "", "a missing persona degrades to no persona"
    assert "agent-gone-0000" in caplog.text, "and says so"


@pytest.mark.asyncio
async def test_no_persona_resolves_to_empty_without_touching_the_db():
    """The default path must not cost a query."""
    from sdk.agent.graph_engine import DAGExecutionEngine

    engine = DAGExecutionEngine()
    with patch("python.harness.db.fetchrow", new_callable=AsyncMock) as fetchrow:
        assert await engine._resolve_persona_prompt(None) == ""
        assert await engine._resolve_persona_prompt("") == ""
    fetchrow.assert_not_called()

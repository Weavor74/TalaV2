"""
sdk/agent/swarm_engine.py
=========================
Engine to process Swarm Topologies, running agents iteratively and evaluating gates.

Concurrency model
-----------------
A topology is a DAG, so a swarm genuinely fans out (a node with several
outgoing edges dispatches all of them at once) and genuinely fans in (a node
with several incoming edges runs once, after the last of them arrives). Three
pieces of bookkeeping make that safe, and all three are single atomic SQL
statements rather than read-modify-write in Python, because every branch of a
swarm is racing every other branch by construction. They live in their own
tables (``swarm_join_arrivals``, ``swarm_run_state``, ``swarm_gate_retries``)
rather than in ``tasks.payload``: that column is double-encoded — a jsonb codec
is registered per connection while callers pass ``json.dumps(...)`` strings, so
payloads are stored as JSON *strings* that jsonb operators cannot address — and
``enqueue_task``'s ON CONFLICT replaces payload wholesale, which would reset any
counter kept inside it.

* ``swarm_join_arrivals`` -- one row per join node holding the set of upstream
  nodes that have arrived. Postgres serialises concurrent upserts of the same
  row, so exactly one arrival sees the set become complete and dispatches the
  join node. The others see a short set and stop.
* ``in_flight`` -- a counter of dispatched-but-unfinished nodes. Successors are
  dispatched (incrementing it) *before* the completing node decrements it, so
  it can only reach zero when the graph really has no frontier left. Whoever
  decrements it to zero finalises the run. This replaces an earlier
  ``SELECT count(*) ... WHERE status IN ('pending','in_progress')`` check that
  had a window where a parallel branch could observe zero between one node
  completing and its successors being enqueued, and synthesise early.
* ``gate_retries`` -- incremented in SQL and the *returned* value is what the
  max_retries decision reads, so two branches failing the same gate at once
  cannot both see the same pre-increment count.

Finalisation is additionally claimed under a one-shot payload flag, so even if
the counter were wrong the synthesis LLM call can only ever fire once.
"""
import asyncio
import logging
import json
from typing import Any, Dict

from sdk.agent.topology import TopologyRegistry, SwarmTopology, TopologyNode
from sdk.agent.queue import BatchQueue, TaskStatus
from sdk.agent.persistence import generate_deterministic_id
from sdk.agent.gated_verifier import GatedVerifier
from sdk.agent.synthesizer import SynthesizerWorker
from python.harness import db

log = logging.getLogger(__name__)

class SwarmEngine:
    """
    Orchestrates execution of tasks according to a SwarmTopology graph.
    """

    @classmethod
    def _db_available(cls) -> bool:
        """True when a live Postgres pool is initialised (matches the
        db_available guard pattern used throughout this codebase)."""
        try:
            db.get_pool()
            return True
        except RuntimeError:
            return False

    # ------------------------------------------------------------------
    # Atomic parent-payload bookkeeping
    # ------------------------------------------------------------------

    @classmethod
    async def _record_arrival(cls, parent_task_id: str, to_node: str, from_node: str) -> set[str]:
        """
        Record that *from_node* satisfied its edge into *to_node*, and return
        the full set of upstream nodes that have arrived so far.

        One statement, so it is atomic: the ON CONFLICT DO UPDATE takes a row
        lock, concurrent arrivals serialise behind it, and RETURNING gives each
        caller the post-update state including its own contribution. The
        array_append is guarded so a retried upstream arriving twice cannot
        over-count the join.
        """
        row = await db.fetchrow(
            """
            INSERT INTO swarm_join_arrivals (parent_task_id, to_node, arrived)
            VALUES ($1, $2, ARRAY[$3::text])
            ON CONFLICT (parent_task_id, to_node) DO UPDATE
                SET arrived = CASE
                        WHEN $3::text = ANY(swarm_join_arrivals.arrived)
                            THEN swarm_join_arrivals.arrived
                        ELSE array_append(swarm_join_arrivals.arrived, $3::text)
                    END,
                    updated_at = NOW()
            RETURNING arrived
            """,
            parent_task_id, to_node, from_node,
        )
        return set(row["arrived"]) if row and row["arrived"] else {from_node}

    @classmethod
    async def _add_in_flight(cls, parent_task_id: str, delta: int) -> int:
        """Atomically adjust the run's in-flight node count and return the new value."""
        row = await db.fetchrow(
            """
            INSERT INTO swarm_run_state (parent_task_id, in_flight)
            VALUES ($1, $2)
            ON CONFLICT (parent_task_id) DO UPDATE
                SET in_flight = swarm_run_state.in_flight + $2
            RETURNING in_flight
            """,
            parent_task_id, delta,
        )
        return int(row["in_flight"]) if row and row["in_flight"] is not None else 0

    @classmethod
    async def _bump_gate_retry(cls, parent_task_id: str, rule_name: str) -> int:
        """Atomically increment a gate's retry counter and return the new count."""
        row = await db.fetchrow(
            """
            INSERT INTO swarm_gate_retries (parent_task_id, rule_name, attempts)
            VALUES ($1, $2, 1)
            ON CONFLICT (parent_task_id, rule_name) DO UPDATE
                SET attempts = swarm_gate_retries.attempts + 1
            RETURNING attempts
            """,
            parent_task_id, rule_name,
        )
        return int(row["attempts"]) if row and row["attempts"] is not None else 1

    @classmethod
    async def _claim_finalisation(cls, parent_task_id: str) -> bool:
        """
        One-shot claim on finalising the run. Returns True for exactly one
        caller: the conditional DO UPDATE matches no row once `finalised` is
        already true, so every later caller gets nothing back.
        """
        row = await db.fetchrow(
            """
            INSERT INTO swarm_run_state (parent_task_id, finalised)
            VALUES ($1, TRUE)
            ON CONFLICT (parent_task_id) DO UPDATE
                SET finalised = TRUE
                WHERE swarm_run_state.finalised = FALSE
            RETURNING parent_task_id
            """,
            parent_task_id,
        )
        return row is not None

    @classmethod
    async def _reset_run_state(cls, parent_task_id: str) -> None:
        """
        Clear a run's bookkeeping before (re)spawning it.

        Parent task ids are deterministic in the goal text and topology, so
        re-running the same swarm reuses the same id — and `enqueue_task`'s
        ON CONFLICT already resets the task rows. Without the same reset here
        the second run inherits the first run's `finalised = TRUE` and its
        leftover join arrivals, so it would execute and then never close.
        """
        await db.execute("DELETE FROM swarm_join_arrivals WHERE parent_task_id = $1", parent_task_id)
        await db.execute("DELETE FROM swarm_gate_retries WHERE parent_task_id = $1", parent_task_id)
        await db.execute(
            """
            INSERT INTO swarm_run_state (parent_task_id, in_flight, finalised)
            VALUES ($1, 0, FALSE)
            ON CONFLICT (parent_task_id) DO UPDATE
                SET in_flight = 0, finalised = FALSE
            """,
            parent_task_id,
        )

    @classmethod
    async def _claim_node(cls, task_id: str) -> bool:
        """
        Atomically move a queued node from 'pending' to 'in_progress'.

        Returns False when another dispatcher already claimed it -- the guard
        against a node with converging edges being executed twice, since
        `generate_deterministic_id` gives both dispatchers the same task id.
        `BatchQueue.transition_task` cannot do this: its UPDATE is
        unconditional and ignores `from_status`, so it is a write, not a
        compare-and-swap.
        """
        row = await db.fetchrow(
            "UPDATE tasks SET status = 'in_progress' WHERE id = $1 AND status = 'pending' RETURNING id",
            task_id,
        )
        return row is not None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    @classmethod
    def _node_task_id(cls, parent_task_id: str, node_id: str, suffix: str = "") -> str:
        return generate_deterministic_id(parent_task_id + node_id + suffix, f"swarm-node-{node_id}{suffix}")

    @classmethod
    async def _dispatch_node(
        cls,
        parent_task_id: str,
        topology: SwarmTopology,
        node: TopologyNode,
        *,
        task_id: str | None = None,
        arrived_from: str | None = None,
        retry_feedback: str | None = None,
        extra_payload: Dict[str, Any] | None = None,
        description: str | None = None,
    ) -> bool:
        """
        Schedule one topology node, honouring its join.

        Returns True when the node was actually dispatched (and therefore
        counted as in-flight). Returns False when the node is a join that is
        still waiting on other upstreams, or when another dispatcher already
        claimed it -- in both cases the caller must not count it.
        """
        incoming = topology.incoming_edges(node.id)
        db_live = cls._db_available()

        # A join only runs once every incoming edge has been satisfied. Nodes
        # with zero or one incoming edge have nothing to wait for, so they skip
        # the barrier entirely -- that keeps plain chains free of bookkeeping
        # and keeps them working when no DB pool is up.
        if db_live and arrived_from is not None and len(incoming) > 1:
            arrivals = await cls._record_arrival(parent_task_id, node.id, arrived_from)
            expected = {edge.from_node for edge in incoming}
            if not expected.issubset(arrivals):
                log.info(
                    "SwarmEngine: join '%s' waiting — arrived %s of %s",
                    node.id, sorted(arrivals), sorted(expected),
                )
                return False
            log.info("SwarmEngine: join '%s' satisfied by %s — dispatching", node.id, sorted(arrivals))

        task_id = task_id or cls._node_task_id(parent_task_id, node.id)
        description = description or node.description
        payload: Dict[str, Any] = {"node_id": node.id, "role": node.role}
        if node.agent_id:
            payload["agent_id"] = node.agent_id
        if extra_payload:
            payload.update(extra_payload)

        await BatchQueue.enqueue_task(
            task_id=task_id,
            description=f"[{node.role.upper()}] {description}",
            status="pending",
            parent_id=parent_task_id,
            payload=payload,
        )

        # Without the rest of this method the queue row is all that ever
        # happened: nothing consumed it, so a started swarm sat at "pending"
        # forever. Gated on DB availability (matching every other
        # db_available guard in this module/BatchQueue) so pure unit tests
        # that mock BatchQueue.enqueue_task without a live pool do not spawn
        # real background execution.
        if not db_live:
            return False

        if not await cls._claim_node(task_id):
            log.info("SwarmEngine: node '%s' (task %s) already claimed — not dispatching twice", node.id, task_id)
            return False

        if node.agent_id:
            # Also written to the column so the task board and any other
            # consumer can see which persona a node is running as, not just
            # the executor reading it back out of the payload.
            await db.execute(
                "UPDATE tasks SET assigned_agent_id = $1 WHERE id = $2", node.agent_id, task_id
            )

        # Counted before execution starts, and therefore before the node that
        # dispatched it decrements itself. See the module docstring.
        await cls._add_in_flight(parent_task_id, 1)
        asyncio.create_task(
            cls.execute_node_task(
                task_id, parent_task_id, node.id, node.role, description,
                retry_feedback=retry_feedback, agent_id=node.agent_id,
            )
        )
        return True

    @classmethod
    async def spawn_topology(cls, goal_text: str, topology_id: str) -> str:
        """
        Instantiate a new swarm run based on a topology.
        Creates a parent task, and dispatches every start node concurrently.
        """
        topology = TopologyRegistry.get(topology_id)
        if not topology:
            raise ValueError(f"Unknown topology: {topology_id}")

        parent_id = generate_deterministic_id(goal_text + topology_id, "swarm-parent")

        # The parent's payload holds only the run's identity. Its live
        # bookkeeping lives in swarm_run_state / swarm_join_arrivals /
        # swarm_gate_retries instead, both because those need atomic SQL and
        # because enqueue_task's ON CONFLICT replaces payload wholesale — any
        # counter kept in there would be silently reset by a re-enqueue.
        await BatchQueue.enqueue_task(
            task_id=parent_id,
            description=f"Swarm Goal: {goal_text}",
            status="in_progress",
            payload={"topology_id": topology_id, "goal": goal_text}
        )

        start_nodes = topology.start_nodes()
        log.info(
            "SwarmEngine: Spawning topology '%s' with %d start node(s) %s",
            topology_id, len(start_nodes), [n.id for n in start_nodes],
        )

        db_live = cls._db_available()
        if db_live:
            await cls._reset_run_state(parent_id)

        # Hold one synthetic in-flight count across the whole spawn. Without
        # it, a fast first start node could complete and decrement the counter
        # to zero before the second start node had been dispatched, finalising
        # the run before it had properly begun.
        if db_live:
            await cls._add_in_flight(parent_id, 1)

        try:
            for node in start_nodes:
                await cls._dispatch_node(parent_id, topology, node)
        finally:
            if db_live:
                remaining = await cls._add_in_flight(parent_id, -1)
                if remaining <= 0:
                    # Degenerate case: nothing dispatched at all.
                    await cls._finalise_run(parent_id)

        return parent_id

    @classmethod
    async def execute_node_task(
        cls,
        task_id: str,
        parent_task_id: str,
        node_id: str,
        role: str,
        description: str,
        retry_feedback: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """
        Actually perform a swarm node's work, persist the outcome, and advance
        the topology graph.

        Runs the node through the same DAGExecutionEngine used by the
        top-level GoalEngine (the one execution substrate in this codebase
        that's actually exercised in production), so a swarm node gets real
        research/synthesis/writing/validation work done against the live
        workspace, not a stub.

        Deliberately does NOT let DAGExecutionEngine spin up its own git
        worktree/branch per node (its default behavior when no worktree_path
        is pre-set — see graph_engine.execute_task_as_dag). Swarm nodes run
        directly against the shared local workspace instead: no git
        dependency, no per-node branch, and — as a direct consequence — a
        later node in the same swarm run (e.g. a "tester" node) actually
        sees an earlier node's (e.g. "coder") file writes immediately,
        instead of them being stranded in a worktree that gets merged back
        after the fact.

        The task row is already 'in_progress' by the time this runs:
        `_dispatch_node` claims it atomically, which is what makes a fan-in
        node safe to have two dispatchers.

        Intended to be launched fire-and-forget via asyncio.create_task; it
        never raises — all failure paths degrade to a failed-result gate
        evaluation so the topology's failure_policy (retry/route_to/blocked)
        still gets a chance to run instead of leaving the branch stuck.
        """
        result: Dict[str, Any] = {"success": False, "output": "", "error": None}
        try:
            from sdk.agent.planner import TaskItem
            prompt_description = f"[{role.upper()}] {description}"
            if retry_feedback:
                prompt_description += f"\n\nAddress this feedback from the previous attempt:\n{retry_feedback}"

            task_item = TaskItem(task_id=task_id, description=prompt_description, dependencies=[])
            task_item.payload = {
                "role": role,
                "node_id": node_id,
                "parent_task_id": parent_task_id,
                # Read by DAGExecutionEngine to resolve this node's persona.
                "agent_id": agent_id,
            }

            from sdk.agent.graph_engine import DAGExecutionEngine
            engine = DAGExecutionEngine()
            # Pre-setting worktree_path short-circuits execute_task_as_dag's
            # own worktree_path resolution (DB lookup, then auto `git
            # worktree add` via handle_task_create_worktree) — it only
            # triggers that path when this attribute is falsy/unset.
            task_item.worktree_path = engine.root_dir
            dag_result = await engine.execute_task_as_dag(task_item)

            result = {
                "success": bool(dag_result.get("success")),
                "output": dag_result.get("output") or dag_result.get("stdout") or "",
                "error": dag_result.get("error"),
            }
        except Exception as exc:
            log.exception(
                "SwarmEngine.execute_node_task: node '%s' (task %s) crashed during execution", node_id, task_id
            )
            result = {"success": False, "output": "", "error": str(exc)}

        to_status = TaskStatus.COMPLETE if result["success"] else TaskStatus.FAILED
        try:
            await BatchQueue.transition_task(task_id, TaskStatus.IN_PROGRESS, to_status, result=result)
        except Exception:
            log.exception("SwarmEngine.execute_node_task: failed to persist final status for task %s", task_id)

        try:
            await cls.evaluate_edges(parent_task_id, node_id, result)
        except Exception:
            log.exception(
                "SwarmEngine.execute_node_task: evaluate_edges crashed for node '%s' (parent %s)",
                node_id, parent_task_id,
            )

    @classmethod
    async def evaluate_edges(cls, parent_task_id: str, completed_node_id: str, result: Dict[str, Any]) -> None:
        """
        Called when a node task completes. Evaluates outgoing edges and gates,
        dispatches whatever the result unlocks, then retires this node from the
        in-flight count — finalising the run if it was the last one.
        """
        from sdk.agent.persistence import TaskEventStore

        # Everything below runs inside the try, because the `finally` is the only
        # place a completing node is retired from the in-flight count.
        #
        # The two early returns -- no parent row, and a topology that is no
        # longer registered -- used to sit ABOVE the try, so hitting either one
        # skipped the decrement entirely. The node had already been counted at
        # dispatch, so the counter could never reach zero again: _finalise_run
        # never fired, no synthesis ran, and the parent sat at in_progress until
        # the reaper marked it failed half an hour later.
        #
        # The topology branch is reachable in normal use. TopologyRegistry.reset()
        # reloads from disk, and the renderer calls swarm.topology.list with
        # reload:true every time the Topologies tab is opened. A user-authored
        # topology has no BUILTIN_TOPOLOGIES fallback, so if its node file is
        # deleted or fails validation mid-run, every subsequent completion leaks
        # its count.
        db_live = cls._db_available()
        halted = False
        outgoing_edges: list = []

        try:
            # Load topology
            query = "SELECT payload FROM tasks WHERE id = $1"
            rows = await db.fetch(query, parent_task_id)
            if not rows:
                log.warning(
                    "SwarmEngine: no parent row for %s while retiring node '%s'; "
                    "retiring it anyway so the run can still finalise",
                    parent_task_id, completed_node_id,
                )
                return

            parent_payload = rows[0]["payload"]
            if isinstance(parent_payload, str):
                parent_payload = json.loads(parent_payload)

            topology_id = parent_payload.get("topology_id")
            topology = TopologyRegistry.get(topology_id)
            if not topology:
                log.warning(
                    "SwarmEngine: topology %r is no longer registered while retiring node "
                    "'%s' of %s; no successors can be dispatched, but the node is still "
                    "retired so the run finalises instead of stalling",
                    topology_id, completed_node_id, parent_task_id,
                )
                return

            outgoing_edges = topology.outgoing_edges(completed_node_id)

            for edge in outgoing_edges:
                passed, feedback, rule_name = await GatedVerifier.verify(edge.condition, {}, result)

                if passed:
                    log.info(f"SwarmEngine: Edge {edge.from_node} -> {edge.to_node} passed gate '{rule_name}'.")
                    target_node = topology.node(edge.to_node) or TopologyNode(
                        id=edge.to_node, role="worker", description=f"Swarm Node: {edge.to_node}"
                    )
                    await cls._dispatch_node(
                        parent_task_id, topology, target_node, arrived_from=completed_node_id
                    )
                    continue

                log.warning(f"SwarmEngine: Edge {edge.from_node} -> {edge.to_node} FAILED gate '{rule_name}'.")

                # Emit event
                await TaskEventStore.append_event(
                    parent_task_id,
                    "gate_failed",
                    {"from_node": edge.from_node, "to_node": edge.to_node, "rule": rule_name, "feedback": feedback}
                )

                policy = topology.failure_policy.get(rule_name, {})
                if not policy:
                    log.error(f"SwarmEngine: No failure policy defined for rule '{rule_name}'. Halting branch.")
                    continue

                requires_human = policy.get("requires_human", False)
                max_retries = policy.get("max_retries", 0)
                route_to = policy.get("route_to")

                if requires_human:
                    log.warning("SwarmEngine: Policy violation requires human intervention. Halting to Blocked.")
                    await BatchQueue.transition_task(
                        parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, reason=feedback
                    )
                    halted = True
                    return

                # Incremented in SQL, and the returned value is what the limit
                # check reads — two branches failing the same gate at once
                # would otherwise both read the same pre-increment count and
                # each get a full retry budget.
                attempt = (
                    await cls._bump_gate_retry(parent_task_id, rule_name)
                    if db_live
                    else parent_payload.get("gate_retries", {}).get(rule_name, 0) + 1
                )

                if attempt > max_retries:
                    log.warning(
                        "SwarmEngine: Max retries (%s) exhausted for gate '%s'. Routing to Blocked/Review.",
                        max_retries, rule_name,
                    )
                    await BatchQueue.transition_task(
                        parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED,
                        reason=f"Max retries exhausted: {feedback}",
                    )
                    halted = True
                    return

                if not route_to or route_to == "blocked":
                    continue

                fallback_node = topology.node(route_to) or TopologyNode(
                    id=route_to, role="worker", description=f"Swarm Node: {route_to}"
                )
                log.info(
                    "SwarmEngine: Routing back to '%s' (Retry %s/%s).", route_to, attempt, max_retries
                )
                verifier_feedback = feedback if policy.get("append_feedback") else None
                await cls._dispatch_node(
                    parent_task_id,
                    topology,
                    fallback_node,
                    # A retry is a fresh attempt at the same node, so it needs
                    # its own task id rather than colliding with the completed
                    # one — and it must bypass the join barrier, since its
                    # upstreams already arrived on the first pass.
                    task_id=cls._node_task_id(parent_task_id, route_to, f"-retry-{attempt}"),
                    retry_feedback=verifier_feedback,
                    extra_payload={
                        "retry": True,
                        "retry_count": attempt,
                        **({"verifier_feedback": verifier_feedback} if verifier_feedback else {}),
                    },
                    description=f"[RETRY {attempt}] {fallback_node.description}",
                )
        finally:
            # This node is done. Retire it *after* everything it unlocked has
            # been dispatched and counted, so the counter can only hit zero
            # when the graph truly has no frontier left.
            if db_live and not halted:
                remaining = await cls._add_in_flight(parent_task_id, -1)
                log.info(
                    "SwarmEngine: node '%s' retired, %d node(s) still in flight for %s",
                    completed_node_id, remaining, parent_task_id,
                )
                if remaining <= 0:
                    await cls._finalise_run(parent_task_id)
            elif not db_live and not outgoing_edges:
                # No pool (unit-test / degraded path): fall back to the
                # terminal-leaf heuristic so behaviour is still sensible.
                await cls._finalise_run(parent_task_id)

    @classmethod
    async def _finalise_run(cls, parent_task_id: str) -> None:
        """
        Aggregate every completed node's output and close the run.

        Claimed one-shot: only the caller that wins `_claim_finalisation` gets
        to run the synthesis LLM call and transition the parent, so converging
        branches cannot produce two reports for one run.
        """
        if cls._db_available() and not await cls._claim_finalisation(parent_task_id):
            log.info("SwarmEngine: finalisation for %s already claimed — skipping", parent_task_id)
            return

        log.info("SwarmEngine: all nodes finished for %s — triggering SynthesizerWorker.", parent_task_id)

        parent_rows = await db.fetch("SELECT payload FROM tasks WHERE id = $1", parent_task_id)
        parent_payload: Dict[str, Any] = {}
        if parent_rows:
            parent_payload = parent_rows[0]["payload"]
            if isinstance(parent_payload, str):
                parent_payload = json.loads(parent_payload)

        # Failed nodes are collected alongside completed ones. A fan-out swarm
        # whose branches can die independently must not report only the
        # survivors: reading just status='complete' made a dead branch vanish
        # from the synthesis entirely, so the run read as a clean success that
        # was quietly missing a whole dimension of the work.
        results_query = (
            "SELECT payload, result, status FROM tasks "
            "WHERE parent_id = $1 AND status IN ('complete', 'failed')"
        )
        completed_tasks = await db.fetch(results_query, parent_task_id)

        upstream_results = []
        for ct in completed_tasks:
            payload = ct["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            node_id = (payload or {}).get("node_id", "unknown")

            res = ct["result"]
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    res = {"output": res, "success": True}
            elif res is None:
                res = {"output": "No result recorded.", "success": False}

            res["node_id"] = node_id
            if ct["status"] == "failed":
                res["success"] = False
                if not res.get("output"):
                    res["output"] = f"Node '{node_id}' failed: {res.get('error') or 'no error recorded'}"
            upstream_results.append(res)

        from sdk.agent.synthesizer import SynthesizerWorker
        goal_text = parent_payload.get("goal", "Unknown Goal")
        final_synthesis = await SynthesizerWorker.synthesize(goal_text, upstream_results)

        # transition_task's own UPDATE always writes its `result` param (defaulting
        # to {} when omitted) — final_synthesis must be passed explicitly here or
        # the parent task's result column silently ends up as '{}'.
        await BatchQueue.transition_task(
            parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE, result=final_synthesis
        )
        log.info(f"SwarmEngine: Swarm {parent_task_id} fully complete.")


class ZombieReaper:
    """
    Reclaims zombie workers and processes that have timed out.
    """
    @classmethod
    async def reclaim_zombies(cls, timeout_minutes: int = 30) -> int:
        from python.harness import db
        from sdk.agent.queue import BatchQueue, TaskStatus
        from datetime import datetime, timezone, timedelta

        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if not db_available:
            return 0

        query = """
            SELECT t.id,
                   (SELECT created_at FROM task_event_store WHERE task_id = t.id ORDER BY created_at DESC LIMIT 1) as last_event_time
            FROM tasks t
            WHERE t.status IN ('in_progress')
        """
        rows = await db.fetch(query)
        reclaimed_count = 0
        now = datetime.now(timezone.utc)

        try:
            from sdk.agent.goal_engine import get_active_task_ids
            active_ids = get_active_task_ids()
        except Exception:
            active_ids = set()

        for row in rows:
            task_id = row["id"]
            if task_id in active_ids:
                # Task is actively executing in memory! Do not mark as zombie!
                continue

            last_event_time = row["last_event_time"]
            if not last_event_time:
                continue

            if (now - last_event_time) > timedelta(minutes=timeout_minutes):
                log.warning(f"ZombieReaper: Reclaiming zombie task {task_id}. No heartbeat for {timeout_minutes} minutes")
                await BatchQueue.transition_task(
                    task_id=task_id,
                    from_status=TaskStatus.IN_PROGRESS,
                    to_status=TaskStatus.FAILED,
                    reason=f"Zombie Reclaim: No worker heartbeat for {timeout_minutes} minutes"
                )
                reclaimed_count += 1

        return reclaimed_count

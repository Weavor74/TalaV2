"""
sdk/agent/swarm_engine.py
=========================
Engine to process Swarm Topologies, running agents iteratively and evaluating gates.
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

    @classmethod
    async def spawn_topology(cls, goal_text: str, topology_id: str) -> str:
        """
        Instantiate a new swarm run based on a topology.
        Creates a parent task, and enqueues the starting nodes.
        """
        topology = TopologyRegistry.get(topology_id)
        if not topology:
            raise ValueError(f"Unknown topology: {topology_id}")

        parent_id = generate_deterministic_id(goal_text + topology_id, "swarm-parent")

        # Enqueue parent
        await BatchQueue.enqueue_task(
            task_id=parent_id,
            description=f"Swarm Goal: {goal_text}",
            status="in_progress",
            payload={"topology_id": topology_id, "goal": goal_text}
        )

        # Find start nodes (nodes with no incoming edges)
        target_nodes = {edge.to_node for edge in topology.edges}
        start_nodes = [node for node in topology.nodes if node.id not in target_nodes]

        log.info(f"SwarmEngine: Spawning topology '{topology_id}' with start nodes {[n.id for n in start_nodes]}")

        # Enqueue start nodes in parallel
        for node in start_nodes:
            task_id = generate_deterministic_id(parent_id + node.id, f"swarm-node-{node.id}")
            await BatchQueue.enqueue_task(
                task_id=task_id,
                description=f"[{node.role.upper()}] {node.description}",
                status="pending",
                parent_id=parent_id,
                payload={"node_id": node.id, "role": node.role}
            )

            # Actually drive the node to execution. Without this, spawn_topology
            # only ever wrote rows to the queue — nothing consumed them, so a
            # started swarm sat at "pending" forever. Gated on DB availability
            # (matches every other db_available guard in this module/BatchQueue)
            # so pure unit tests that mock BatchQueue.enqueue_task without a
            # live pool don't spawn real background execution.
            if cls._db_available():
                asyncio.create_task(
                    cls.execute_node_task(task_id, parent_id, node.id, node.role, node.description)
                )

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
    ) -> None:
        """
        Actually perform a swarm node's work, persist the outcome, and advance
        the topology graph. This is the piece that was entirely missing:
        spawn_topology/evaluate_edges previously only ever wrote 'pending'
        rows to the tasks table — nothing executed them or called
        evaluate_edges to move the graph forward.

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

        Intended to be launched fire-and-forget via asyncio.create_task; it
        never raises — all failure paths degrade to a failed-result gate
        evaluation so the topology's failure_policy (retry/route_to/blocked)
        still gets a chance to run instead of leaving the branch stuck.
        """
        result: Dict[str, Any] = {"success": False, "output": "", "error": None}
        try:
            await BatchQueue.transition_task(task_id, TaskStatus.PENDING, TaskStatus.IN_PROGRESS)

            from sdk.agent.planner import TaskItem
            prompt_description = f"[{role.upper()}] {description}"
            if retry_feedback:
                prompt_description += f"\n\nAddress this feedback from the previous attempt:\n{retry_feedback}"

            task_item = TaskItem(task_id=task_id, description=prompt_description, dependencies=[])
            task_item.payload = {"role": role, "node_id": node_id, "parent_task_id": parent_task_id}

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
        Called when a node task completes. Evaluates outgoing edges and gates.
        """
        from sdk.agent.persistence import TaskEventStore
        
        # Load topology
        query = "SELECT payload FROM tasks WHERE id = $1"
        rows = await db.fetch(query, parent_task_id)
        if not rows:
            return
            
        parent_payload = rows[0]["payload"]
        if isinstance(parent_payload, str):
            parent_payload = json.loads(parent_payload)
            
        topology_id = parent_payload.get("topology_id")
        topology = TopologyRegistry.get(topology_id)
        if not topology:
            return
            
        # Retries state is tracked in parent payload
        gate_retries = parent_payload.get("gate_retries", {})
            
        outgoing_edges = [e for e in topology.edges if e.from_node == completed_node_id]
        
        for edge in outgoing_edges:
            passed, feedback, rule_name = await GatedVerifier.verify(edge.condition, {}, result)
            
            if passed:
                log.info(f"SwarmEngine: Edge {edge.from_node} -> {edge.to_node} passed gate '{rule_name}'.")
                
                target_node = next((n for n in topology.nodes if n.id == edge.to_node), None)
                next_task_id = generate_deterministic_id(parent_task_id + edge.to_node, f"swarm-node-{edge.to_node}")
                next_role = target_node.role if target_node else "worker"
                next_description = target_node.description if target_node else f"Swarm Node: {edge.to_node}"
                await BatchQueue.enqueue_task(
                    task_id=next_task_id,
                    description=f"[{next_role.upper()}] {next_description}",
                    status="pending",
                    parent_id=parent_task_id,
                    payload={"node_id": edge.to_node, "role": next_role}
                )

                # Drive the next node forward — evaluate_edges is the only
                # place that discovers a topology's downstream nodes, so this
                # is where progression has to be re-triggered or the graph
                # stalls after one hop.
                if cls._db_available():
                    asyncio.create_task(
                        cls.execute_node_task(next_task_id, parent_task_id, edge.to_node, next_role, next_description)
                    )
            else:
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
                current_retries = gate_retries.get(rule_name, 0)
                route_to = policy.get("route_to")
                target_status = policy.get("status", "pending")
                
                if requires_human:
                    log.warning(f"SwarmEngine: Policy violation requires human intervention. Halting to Blocked.")
                    await BatchQueue.transition_task(parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, reason=feedback)
                    return
                    
                if current_retries >= max_retries:
                    log.warning(f"SwarmEngine: Max retries ({max_retries}) exhausted for gate '{rule_name}'. Routing to Blocked/Review.")
                    await BatchQueue.transition_task(parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, reason=f"Max retries exhausted: {feedback}")
                    return
                    
                # Increment retries
                gate_retries[rule_name] = current_retries + 1
                parent_payload["gate_retries"] = gate_retries
                await db.execute("UPDATE tasks SET payload = $1::jsonb WHERE id = $2", json.dumps(parent_payload), parent_task_id)
                
                if route_to:
                    fallback_node = route_to
                    log.info(f"SwarmEngine: Routing back to '{fallback_node}' (Retry {current_retries+1}/{max_retries}).")
                    fallback_task_id = generate_deterministic_id(parent_task_id + fallback_node + str(current_retries), f"swarm-node-{fallback_node}-{current_retries}")

                    fallback_payload = {"node_id": fallback_node, "retry": True, "retry_count": current_retries + 1}
                    verifier_feedback = feedback if policy.get("append_feedback") else None
                    if verifier_feedback:
                        fallback_payload["verifier_feedback"] = verifier_feedback

                    await BatchQueue.enqueue_task(
                        task_id=fallback_task_id,
                        description=f"[RETRY {current_retries+1}] Swarm Node: {fallback_node}",
                        status="pending",
                        parent_id=parent_task_id,
                        payload=fallback_payload
                    )

                    if cls._db_available():
                        fallback_target = next((n for n in topology.nodes if n.id == fallback_node), None)
                        fallback_role = fallback_target.role if fallback_target else "worker"
                        fallback_description = fallback_target.description if fallback_target else f"Swarm Node: {fallback_node}"
                        asyncio.create_task(
                            cls.execute_node_task(
                                fallback_task_id, parent_task_id, fallback_node, fallback_role,
                                fallback_description, retry_feedback=verifier_feedback,
                            )
                        )


        # Check if terminal node and possibly end the swarm
        if not outgoing_edges:
            log.info(f"SwarmEngine: Node {completed_node_id} is a terminal leaf. Checking for swarm completion.")
            active_query = "SELECT count(id) as c FROM tasks WHERE parent_id = $1 AND status IN ('pending', 'in_progress', 'review')"
            active_count = await db.fetch(active_query, parent_task_id)
            if active_count and active_count[0]["c"] == 0:
                log.info(f"SwarmEngine: All sibling tasks finished. Triggering SynthesizerWorker for parent {parent_task_id}.")
                
                results_query = "SELECT payload, result FROM tasks WHERE parent_id = $1 AND status = 'complete'"
                completed_tasks = await db.fetch(results_query, parent_task_id)
                
                upstream_results = []
                for ct in completed_tasks:
                    payload = ct["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    node_id = payload.get("node_id", "unknown")
                    
                    res = ct["result"]
                    if isinstance(res, str):
                        try:
                            res = json.loads(res)
                        except:
                            res = {"output": res, "success": True}
                    elif res is None:
                        res = {"output": "No result recorded.", "success": False}
                    
                    res["node_id"] = node_id
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
                log.warning(f"ZombieReaper: Reclaiming zombie task {task_id}. No heartbeat for {timeout_minutes} minutes.")
                await BatchQueue.transition_task(
                    task_id=task_id,
                    from_status=TaskStatus.IN_PROGRESS,
                    to_status=TaskStatus.FAILED,
                    reason=f"Zombie Reclaim: No worker heartbeat for {timeout_minutes} minutes"
                )
                reclaimed_count += 1
                
        return reclaimed_count



# File: `swarm_engine.py`

## Module Description

sdk/agent/swarm_engine.py
=========================
Engine to process Swarm Topologies, running agents iteratively and evaluating gates.

## Classes

### `class SwarmEngine`
Orchestrates execution of tasks according to a SwarmTopology graph.

- **`def spawn_topology(cls, goal_text, topology_id)`**
  - Instantiate a new swarm run based on a topology.
  Creates a parent task, and enqueues the starting nodes.
- **`def evaluate_edges(cls, parent_task_id, completed_node_id, result)`**
  - Called when a node task completes. Evaluates outgoing edges and gates.

### `class ZombieReaper`
Reclaims zombie workers and processes that have timed out.

- **`def reclaim_zombies(cls, timeout_minutes)`**

## Source Code

```python
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
            
        return parent_id
        
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
                await BatchQueue.enqueue_task(
                    task_id=next_task_id,
                    description=f"[{target_node.role.upper()}] {target_node.description}" if target_node else f"Swarm Node: {edge.to_node}",
                    status="pending",
                    parent_id=parent_task_id,
                    payload={"node_id": edge.to_node, "role": target_node.role if target_node else "worker"}
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
                    if policy.get("append_feedback"):
                        fallback_payload["verifier_feedback"] = feedback
                        
                    await BatchQueue.enqueue_task(
                        task_id=fallback_task_id,
                        description=f"[RETRY {current_retries+1}] Swarm Node: {fallback_node}",
                        status="pending",
                        parent_id=parent_task_id,
                        payload=fallback_payload
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
                
                await db.execute("UPDATE tasks SET result = $1::jsonb WHERE id = $2", json.dumps(final_synthesis), parent_task_id)
                await BatchQueue.transition_task(parent_task_id, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE)
                log.info(f"SwarmEngine: Swarm {parent_task_id} fully complete.")


class ZombieReaper:
    """
    Reclaims zombie workers and processes that have timed out.
    """
    @classmethod
    async def reclaim_zombies(cls, timeout_minutes: int = 5) -> int:
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
        
        for row in rows:
            last_event_time = row["last_event_time"]
            if not last_event_time:
                continue
                
            if (now - last_event_time) > timedelta(minutes=timeout_minutes):
                task_id = row["id"]
                log.warning(f"ZombieReaper: Reclaiming zombie task {task_id}. No heartbeat for {timeout_minutes} minutes.")
                await BatchQueue.transition_task(
                    task_id=task_id,
                    from_status=TaskStatus.IN_PROGRESS,
                    to_status=TaskStatus.FAILED,
                    reason=f"Zombie Reclaim: No worker heartbeat for {timeout_minutes} minutes"
                )
                reclaimed_count += 1
                
        return reclaimed_count



```

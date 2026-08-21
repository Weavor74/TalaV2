"""
sdk/agent/topology.py
=====================
Swarm Topology Definition.

A topology is the *shape* of a swarm: which agent roles run, in what order,
which branches run concurrently, and which gate has to pass before an edge is
allowed to advance. The engine that runs one lives in
``sdk/agent/swarm_engine.py``; this module only defines and validates them.

Topologies are authored as HTML-NN files (``data-node-type="swarm-topology"``)
in the active nodes directory -- the node *is* the swarm definition, written on
creation and kept for reuse, exactly like the self-extension chain in
``nodes/workflow-self-extension.html``. ``TopologyRegistry`` loads every such
node on first use. The built-ins registered at the bottom of this file are a
fallback only: if a node is missing or its spec fails validation, the built-in
copy of that topology stays registered and the reason is logged, so a bad edit
reverts the shape rather than breaking the swarm entirely.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Topology, node and edge identifiers are used to build deterministic task
#: ids and jsonb payload keys, so they are restricted to a safe token shape.
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")


class TopologySpecError(ValueError):
    """Raised when a topology spec is structurally invalid."""


@dataclass
class TopologyNode:
    id: str
    role: str
    description: str = ""
    #: Optional persona (an `agents.id`) this node runs as. None means the node
    #: runs with no persona of its own, which is the default. Existence is not
    #: checked when the spec is parsed -- that would make loading a topology
    #: depend on a live database -- so a persona deleted after authoring is
    #: caught and logged at dispatch instead.
    agent_id: str | None = None


@dataclass
class TopologyEdge:
    from_node: str
    to_node: str
    condition: str = "success"  # 'success', 'tests_passed', 'approved', etc.


@dataclass
class SwarmTopology:
    topology_id: str
    name: str
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    failure_policy: dict[str, Any] = field(default_factory=dict)
    #: Set when this topology was loaded from an HTML-NN rather than from the
    #: built-in fallback registrations below. Purely informational -- the UI
    #: shows it so it is obvious which definitions are editable on disk.
    source_node_id: str | None = None
    #: The node's #semantic-memory prose. Carried so the editor can round-trip
    #: what the author wrote instead of silently replacing it with generated
    #: text on the next save.
    semantic_memory: str = ""

    def node(self, node_id: str) -> Optional[TopologyNode]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def incoming_edges(self, node_id: str) -> list[TopologyEdge]:
        """Every edge that has to be satisfied before *node_id* may run."""
        return [e for e in self.edges if e.to_node == node_id]

    def outgoing_edges(self, node_id: str) -> list[TopologyEdge]:
        return [e for e in self.edges if e.from_node == node_id]

    def start_nodes(self) -> list[TopologyNode]:
        """Nodes with no incoming edges -- the swarm's initial frontier.

        More than one start node means the swarm fans out immediately.
        """
        targets = {edge.to_node for edge in self.edges}
        return [node for node in self.nodes if node.id not in targets]

    def to_dict(self) -> dict[str, Any]:
        """The authored spec, exactly as it is stored in the node's #node-state."""
        return {
            "topology_id": self.topology_id,
            "name": self.name,
            "nodes": [
                # agent_id is omitted entirely when unset rather than written
                # as null, so a topology that uses no personas reads as one
                # that has nothing to do with them.
                {
                    "id": n.id,
                    "role": n.role,
                    "description": n.description,
                    **({"agent_id": n.agent_id} if n.agent_id else {}),
                }
                for n in self.nodes
            ],
            # from_node/to_node, not from/to: this dict is both the authored
            # HTML-NN spec and the shape sent over `swarm.topology.list`, and
            # the renderer's edge drawing reads edge.from_node. The loader
            # accepts either spelling; this is the canonical one.
            "edges": [
                {"from_node": e.from_node, "to_node": e.to_node, "condition": e.condition}
                for e in self.edges
            ],
            "failure_policy": self.failure_policy,
        }

    def to_wire(self) -> dict[str, Any]:
        """The spec plus where it came from — what the editor needs to show it.

        Kept separate from `to_dict` so these load-time annotations never leak
        into the authored spec written back to the node file.
        """
        return {
            **self.to_dict(),
            "source_node_id": self.source_node_id,
            "semantic_memory": self.semantic_memory,
            # Built-ins have no node behind them, so they cannot be edited or
            # deleted on disk; the editor greys those actions out.
            "editable": self.source_node_id is not None,
        }


def _require_id(value: Any, what: str) -> str:
    token = str(value or "").strip()
    if not ID_PATTERN.fullmatch(token):
        raise TopologySpecError(
            f"{what} must be a lowercase token matching {ID_PATTERN.pattern!r}, got {value!r}"
        )
    return token


def _detect_cycle(node_ids: list[str], edges: list[TopologyEdge]) -> list[str] | None:
    """Return one cycle as a node-id path, or None when the graph is acyclic.

    Only the *edge* graph has to be acyclic. ``failure_policy[...]["route_to"]``
    deliberately points backwards -- that is the retry mechanism, not an edge,
    and it is bounded by ``max_retries`` rather than by graph structure.
    """
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        adjacency[edge.from_node].append(edge.to_node)

    UNVISITED, IN_STACK, DONE = 0, 1, 2
    state = dict.fromkeys(node_ids, UNVISITED)
    path: list[str] = []

    def walk(node_id: str) -> list[str] | None:
        state[node_id] = IN_STACK
        path.append(node_id)
        for neighbour in adjacency[node_id]:
            if state[neighbour] == IN_STACK:
                return path[path.index(neighbour):] + [neighbour]
            if state[neighbour] == UNVISITED:
                found = walk(neighbour)
                if found:
                    return found
        path.pop()
        state[node_id] = DONE
        return None

    for node_id in node_ids:
        if state[node_id] == UNVISITED:
            found = walk(node_id)
            if found:
                return found
    return None


def topology_from_spec(spec: Any, *, source_node_id: str | None = None) -> SwarmTopology:
    """
    Build (and fully validate) a SwarmTopology from a plain dict -- the
    ``topology`` object inside an HTML-NN ``#node-state`` block, or the same
    shape arriving over RPC.

    Raises TopologySpecError with a specific reason on anything malformed.
    Callers that must not fail (registry loading) catch it and log; callers
    that should reject bad input (the create RPC) let it propagate.
    """
    if not isinstance(spec, dict):
        raise TopologySpecError("topology spec must be an object")

    topology_id = _require_id(spec.get("topology_id"), "topology_id")
    name = str(spec.get("name") or "").strip()
    if not name:
        raise TopologySpecError(f"topology {topology_id!r}: 'name' is required")

    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise TopologySpecError(f"topology {topology_id!r}: 'nodes' must be a non-empty list")

    nodes: list[TopologyNode] = []
    seen_ids: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise TopologySpecError(f"topology {topology_id!r}: node entry is not an object")
        node_id = _require_id(raw.get("id"), f"topology {topology_id!r}: node id")
        if node_id in seen_ids:
            raise TopologySpecError(f"topology {topology_id!r}: duplicate node id {node_id!r}")
        role = str(raw.get("role") or "").strip()
        if not role:
            raise TopologySpecError(f"topology {topology_id!r}: node {node_id!r} has no role")
        seen_ids.add(node_id)
        # Empty string and null both mean "no persona"; normalise to None so
        # the rest of the system has one representation of absent to test.
        agent_id = str(raw.get("agent_id") or "").strip() or None
        nodes.append(
            TopologyNode(
                id=node_id,
                role=role,
                description=str(raw.get("description") or "").strip(),
                agent_id=agent_id,
            )
        )

    raw_edges = spec.get("edges")
    if not isinstance(raw_edges, list):
        raise TopologySpecError(f"topology {topology_id!r}: 'edges' must be a list")

    # Gate names are validated against the verifier's own rule set: an edge
    # whose condition no rule implements can never pass, so the branch behind
    # it would silently halt on every run. That is precisely the class of
    # silent-stall bug this validation exists to make loud.
    from sdk.agent.gated_verifier import GatedVerifier

    edges: list[TopologyEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise TopologySpecError(f"topology {topology_id!r}: edge entry is not an object")
        # 'from'/'to' is the authored form; 'from_node'/'to_node' matches the
        # dataclass field names and is accepted so a spec hand-copied from
        # Python still loads.
        from_node = _require_id(
            raw.get("from", raw.get("from_node")), f"topology {topology_id!r}: edge 'from'"
        )
        to_node = _require_id(
            raw.get("to", raw.get("to_node")), f"topology {topology_id!r}: edge 'to'"
        )
        for endpoint in (from_node, to_node):
            if endpoint not in seen_ids:
                raise TopologySpecError(
                    f"topology {topology_id!r}: edge references unknown node {endpoint!r}"
                )
        if from_node == to_node:
            raise TopologySpecError(f"topology {topology_id!r}: edge {from_node!r} points at itself")
        if (from_node, to_node) in seen_edges:
            raise TopologySpecError(
                f"topology {topology_id!r}: duplicate edge {from_node!r} -> {to_node!r}"
            )
        seen_edges.add((from_node, to_node))

        condition = str(raw.get("condition") or "success").strip()
        if condition not in GatedVerifier.KNOWN_RULES:
            raise TopologySpecError(
                f"topology {topology_id!r}: edge {from_node!r} -> {to_node!r} has unknown gate "
                f"condition {condition!r} (known: {sorted(GatedVerifier.KNOWN_RULES)})"
            )
        edges.append(TopologyEdge(from_node=from_node, to_node=to_node, condition=condition))

    cycle = _detect_cycle([n.id for n in nodes], edges)
    if cycle:
        raise TopologySpecError(f"topology {topology_id!r}: edges form a cycle {' -> '.join(cycle)}")

    failure_policy = spec.get("failure_policy") or {}
    if not isinstance(failure_policy, dict):
        raise TopologySpecError(f"topology {topology_id!r}: 'failure_policy' must be an object")
    for rule, policy in failure_policy.items():
        if not isinstance(policy, dict):
            raise TopologySpecError(
                f"topology {topology_id!r}: failure_policy[{rule!r}] must be an object"
            )
        route_to = policy.get("route_to")
        # 'blocked' is the documented sentinel for "stop here", not a node id.
        if route_to and route_to != "blocked" and route_to not in seen_ids:
            raise TopologySpecError(
                f"topology {topology_id!r}: failure_policy[{rule!r}].route_to references "
                f"unknown node {route_to!r}"
            )

    topology = SwarmTopology(
        topology_id=topology_id,
        name=name,
        nodes=nodes,
        edges=edges,
        failure_policy=failure_policy,
        source_node_id=source_node_id,
    )

    # A gate that can fail with no policy behind it halts its branch; that is a
    # legitimate choice, so this is a debug note rather than a rejection.
    for edge in edges:
        if edge.condition not in failure_policy:
            log.debug(
                "Topology %r: gate %r on edge %s -> %s has no failure_policy; a failure halts that branch",
                topology_id, edge.condition, edge.from_node, edge.to_node,
            )
    return topology


class TopologyRegistry:
    _topologies: dict[str, SwarmTopology] = {}
    _nodes_loaded: bool = False

    @classmethod
    def register(cls, topology: SwarmTopology):
        cls._topologies[topology.topology_id] = topology

    @classmethod
    def ensure_loaded(cls) -> None:
        """Load HTML-NN topology definitions once, on first read."""
        if cls._nodes_loaded:
            return
        # Set before loading so a failure inside the loader cannot make every
        # subsequent lookup retry a broken filesystem read.
        cls._nodes_loaded = True
        cls.load_from_nodes()

    @classmethod
    def load_from_nodes(cls) -> int:
        """
        (Re)load every swarm-topology HTML-NN from the active nodes directory.

        Returns the number of topologies registered from nodes. Definitions
        that fail validation are skipped with a logged reason, leaving any
        built-in fallback of the same id in place.
        """
        try:
            from sdk.agent.topology_nodes import load_topology_nodes
        except Exception:
            log.exception("TopologyRegistry: topology node loader unavailable -- built-ins only")
            return 0

        loaded = 0
        for topology in load_topology_nodes():
            cls.register(topology)
            loaded += 1
        log.info("TopologyRegistry: loaded %d topology definition(s) from HTML-NN nodes", loaded)
        return loaded

    @classmethod
    def unregister(cls, topology_id: str) -> bool:
        """Drop a topology from the registry. Returns True if it was present."""
        return cls._topologies.pop(topology_id, None) is not None

    @classmethod
    def reload(cls) -> int:
        """Force a re-read of the topology nodes (after one is created/edited)."""
        cls._nodes_loaded = True
        return cls.load_from_nodes()

    @classmethod
    def reset(cls) -> int:
        """
        Rebuild the registry from scratch: built-in fallbacks, then nodes.

        Deleting or renaming a topology node has to be able to *remove* an
        entry, which `reload` alone cannot do -- it only overwrites. Whatever
        survives a reset is exactly what is on disk plus the built-ins.
        """
        cls._topologies.clear()
        for builder in BUILTIN_TOPOLOGIES:
            cls.register(builder())
        cls._nodes_loaded = True
        return cls.load_from_nodes()

    @classmethod
    def get(cls, topology_id: str) -> Optional[SwarmTopology]:
        cls.ensure_loaded()
        return cls._topologies.get(topology_id)

    @classmethod
    def list_all(cls) -> list[SwarmTopology]:
        cls.ensure_loaded()
        return list(cls._topologies.values())


# ---------------------------------------------------------------------------
# Built-in fallbacks
# ---------------------------------------------------------------------------
# These mirror nodes/swarm-topology-*.html. The node files are the source of
# truth and overwrite these on load; these exist so a deleted or malformed
# node degrades to the last-known-good shape instead of leaving the registry
# empty. See the module docstring.
#
# Each is a factory rather than a shared instance so `TopologyRegistry.reset`
# can rebuild them cleanly, and so a caller that mutates one it was handed
# cannot corrupt the fallback everyone else falls back to.


def _builtin_code_repair() -> SwarmTopology:
    return SwarmTopology(
    topology_id="swarm-code-repair",
    name="Code Repair Swarm",
    nodes=[
        TopologyNode(id="planner", role="planner", description="Plan the fix"),
        TopologyNode(id="coder", role="coder", description="Implement the fix"),
        TopologyNode(id="tester", role="verifier", description="Run the tests"),
        TopologyNode(id="reviewer", role="reviewer", description="Review the code"),
        TopologyNode(id="documenter", role="documenter", description="Document the changes")
    ],
    edges=[
        TopologyEdge(from_node="planner", to_node="coder"),
        TopologyEdge(from_node="coder", to_node="tester"),
        TopologyEdge(from_node="tester", to_node="reviewer", condition="tests_passed"),
        TopologyEdge(from_node="reviewer", to_node="documenter", condition="approved")
    ],
    failure_policy={
        "tests_passed": {
            "route_to": "coder",
            "status": "ready",
            "max_retries": 3,
            "append_feedback": True
        },
        "approved": {
            "route_to": "coder",
            "status": "ready",
            "max_retries": 2,
            "append_feedback": True
        },
        "policy_violation": {
            "route_to": "blocked",
            "status": "blocked",
            "requires_human": True
        },
        "missing_artifact": {
            "route_to": "coder",
            "status": "ready",
            "max_retries": 2
        }
    }
)


BUILTIN_TOPOLOGIES = (_builtin_code_repair,)

for _builder in BUILTIN_TOPOLOGIES:
    TopologyRegistry.register(_builder())

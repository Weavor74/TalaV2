# File: `topology.py`

## Module Description

sdk/agent/topology.py
=====================
Swarm Topology Definition

## Classes

### `class TopologyNode`

### `class TopologyEdge`

### `class SwarmTopology`

### `class TopologyRegistry`
- **`def register(cls, topology)`**
- **`def get(cls, topology_id)`**
- **`def list_all(cls)`**

## Source Code

```python
"""
sdk/agent/topology.py
=====================
Swarm Topology Definition
"""
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class TopologyNode:
    id: str
    role: str
    description: str = ""

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

class TopologyRegistry:
    _topologies: dict[str, SwarmTopology] = {}

    @classmethod
    def register(cls, topology: SwarmTopology):
        cls._topologies[topology.topology_id] = topology

    @classmethod
    def get(cls, topology_id: str) -> Optional[SwarmTopology]:
        return cls._topologies.get(topology_id)

    @classmethod
    def list_all(cls) -> list[SwarmTopology]:
        return list(cls._topologies.values())

# Register builtin Code Repair Swarm
TopologyRegistry.register(SwarmTopology(
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
            "route_to": "worker",
            "status": "ready",
            "max_retries": 2
        }
    }
))

```

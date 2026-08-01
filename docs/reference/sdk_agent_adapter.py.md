# File: `adapter.py`

## Module Description

adapter.py — Tala Cognitive Engine Room: HTML-NN LoRA-style Adapter
===================================================================
Compiles activated Neural Nodes into a structured AdapterPacket for the runtime.

## Classes

### `class AdapterPacket`
- **`def from_dict(cls, data)`**

### `class AdapterCompiler`
- **`def compile(cls, nodes)`**

## Source Code

```python
"""
adapter.py — Tala Cognitive Engine Room: HTML-NN LoRA-style Adapter
===================================================================
Compiles activated Neural Nodes into a structured AdapterPacket for the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .parser import NeuralNode

@dataclass
class AdapterPacket:
    facts: list[str] = field(default_factory=list)
    reasoning_rules: list[str] = field(default_factory=list)
    tool_affordances: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    response_style: dict[str, str] = field(default_factory=dict)
    acceptance_criteria: list[str] = field(default_factory=list)
    validators: list[str] = field(default_factory=list)
    authority: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdapterPacket":
        return cls(
            facts=data.get("facts", []),
            reasoning_rules=data.get("reasoning_rules", []),
            tool_affordances=data.get("tool_affordances", []),
            forbidden_actions=data.get("forbidden_actions", []),
            response_style=data.get("response_style", {}),
            acceptance_criteria=data.get("acceptance_criteria", []),
            validators=data.get("validators", []),
            authority=data.get("authority", {})
        )

class AdapterCompiler:
    @classmethod
    def compile(cls, nodes: list["NeuralNode"]) -> AdapterPacket:
        packet = AdapterPacket()
        
        # Sort nodes by execution_authority descending so highest authority takes precedence
        sorted_nodes = sorted(nodes, key=lambda n: getattr(n, "execution_authority", 0.0), reverse=True)
        
        forbidden_set = set()
        
        for node in sorted_nodes:
            if not getattr(node, "adapter", None):
                continue
                
            node_packet: AdapterPacket = node.adapter
            auth = getattr(node, "execution_authority", 0.0)
            
            # Facts and Rules aggregate, removing direct duplicates
            for fact in node_packet.facts:
                if fact not in packet.facts:
                    packet.facts.append(fact)
            for rule in node_packet.reasoning_rules:
                if rule not in packet.reasoning_rules:
                    packet.reasoning_rules.append(rule)
                    
            # Forbidden actions always accumulate (absolute kill-switch)
            for forbidden in node_packet.forbidden_actions:
                forbidden_set.add(forbidden)
                
            # Affordances are added ONLY if they are not in the forbidden set
            for affordance in node_packet.tool_affordances:
                if affordance not in forbidden_set and affordance not in packet.tool_affordances:
                    packet.tool_affordances.append(affordance)
                    
            # Dictionaries (styles) get merged, highest authority wins (which is why we sort desc)
            for k, v in node_packet.response_style.items():
                if k not in packet.response_style:
                    packet.response_style[k] = v
                    
            # Acceptance and validators append
            for criteria in node_packet.acceptance_criteria:
                if criteria not in packet.acceptance_criteria:
                    packet.acceptance_criteria.append(criteria)
            for val in node_packet.validators:
                if val not in packet.validators:
                    packet.validators.append(val)
                    
            packet.authority[getattr(node, "node_id", "unknown")] = auth
            
        # Ensure affordances blocked by lower-authority forbidden lists are removed
        packet.tool_affordances = [a for a in packet.tool_affordances if a not in forbidden_set]
        packet.forbidden_actions = list(forbidden_set)
        
        return packet

```

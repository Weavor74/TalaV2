# File: `trust_registry.py`

## Module Description

trust_registry.py — Tala Cognitive Engine Room: Trust Registry
==============================================================
Manages node authority based on peer trust and key signatures.

## Classes

### `class TrustRegistry`
Enforces trust schemas against executed Neural Nodes using firewall_contacts.

- **`def __init__(self, contacts)`**
- **`def verify_and_cap_nodes(self, nodes)`**
  - Filters and caps nodes based on their cryptographic signatures and our trust schemas.

## Source Code

```python
"""
trust_registry.py — Tala Cognitive Engine Room: Trust Registry
==============================================================
Manages node authority based on peer trust and key signatures.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .parser import NeuralNode

log = logging.getLogger(__name__)

class TrustRegistry:
    """
    Enforces trust schemas against executed Neural Nodes using firewall_contacts.
    """
    def __init__(self, contacts: list[dict[str, Any]]) -> None:
        self.contacts = contacts
        self._key_map: dict[str, dict[str, Any]] = {}
        for c in contacts:
            rights = c.get("access_rights", {})
            fp = rights.get("key_fingerprint")
            if fp:
                self._key_map[fp] = rights

    def verify_and_cap_nodes(self, nodes: list["NeuralNode"]) -> list["NeuralNode"]:
        """
        Filters and caps nodes based on their cryptographic signatures and our trust schemas.
        """
        verified_nodes = []
        for node in nodes:
            provenance = node.node_provenance or {}
            fp = provenance.get("key_fingerprint")
            
            # Quarantine unknown remote executable skills
            if getattr(node, "executable_code", None) and getattr(node, "source", None) == "remote":
                if not fp or fp not in self._key_map:
                    log.warning("Quarantining node %r: unknown remote executable skill", node.node_id)
                    continue
                rights = self._key_map[fp]
                if rights.get("trust_tier") == "unknown":
                    log.warning("Quarantining node %r: remote executable skill from unknown trust tier", node.node_id)
                    continue
            else:
                # Allowed node but key is unknown
                if fp and fp not in self._key_map:
                    log.info("TELEMETRY: AdapterPacket activated node %r from unknown key %r", node.node_id, fp)

            if fp:
                rights = self._key_map.get(fp, {})
                
                if rights.get("revoked", False):
                    log.warning("Rejecting node %r: key is revoked", node.node_id)
                    continue
                
                ceiling = rights.get("authority_ceiling")
                if ceiling is not None:
                    try:
                        node.execution_authority = min(node.execution_authority, float(ceiling))
                    except (TypeError, ValueError):
                        pass

            verified_nodes.append(node)
            
        return verified_nodes

```

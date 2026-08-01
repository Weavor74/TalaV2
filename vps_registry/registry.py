"""
NodeRegistry maintains Subscriber mappings (node_hash -> List[Agent_ID]) on the VPS,
backed by the VPS local ledger.
"""
from __future__ import annotations
import json
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class NodeRegistry:
    """
    NAME
           NodeRegistry - Subscriber mapping registry for VPS node updates
           
    SYNOPSIS
           registry = NodeRegistry(root_dir)
           
    DESCRIPTION
           Manages maps between HTML-NN file content hashes and subscribed Agent IDs.
           Persists mapping configuration to a local ledger file.
    """
    def __init__(self, root_dir: str | None = None) -> None:
        self.root_dir = Path(root_dir or os.getcwd())
        self.registry_file = self.root_dir / "data" / "vps_registry.json"
        self.mapping = {}
        self.load_registry()

    def load_registry(self) -> None:
        if self.registry_file.exists():
            try:
                self.mapping = json.loads(self.registry_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("NodeRegistry: failed to load mapping: %s", e)
                self.mapping = {}
        else:
            self.mapping = {}

    def save_registry(self) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.registry_file.write_text(json.dumps(self.mapping, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error("NodeRegistry: failed to save mapping: %s", e)

    def add_subscriber(self, node_hash: str, agent_id: str) -> None:
        subscribers = self.mapping.setdefault(node_hash, [])
        if agent_id not in subscribers:
            subscribers.append(agent_id)
            self.save_registry()

    def get_subscribers(self, node_hash: str) -> list[str]:
        return self.mapping.get(node_hash, [])

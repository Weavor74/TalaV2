"""
The trust registry.

This file used to also exercise ``python.ledger.chain.LedgerChain`` — a second,
Python chain implementation that duplicated the live Node one on the same port
and had no callers (UI-027). It has been deleted, and the tests that covered it
went with it. ``TrustRegistry`` is live (``server.py`` applies it to contacts)
and is what remains here.
"""

import os
import json
import hashlib
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sdk.agent.trust_registry import TrustRegistry

class MockAdapterPacket:
    def __init__(self):
        self.facts = ["dummy fact"]
        self.reasoning_rules = []
        self.tool_affordances = []
        self.forbidden_actions = []
        self.response_style = {}
        self.acceptance_criteria = []
        self.validators = []
        self.authority = {}

class MockNeuralNode:
    def __init__(self, node_id, source, executable_code=None, key_fingerprint=None, authority=1.0):
        self.node_id = node_id
        self.source = source
        self.executable_code = executable_code
        self.node_provenance = {"key_fingerprint": key_fingerprint} if key_fingerprint else {}
        self.execution_authority = authority
        self.adapter = MockAdapterPacket()

def test_sdk_trust_features(caplog):
    # Setup
    contacts = [
        {"access_rights": {"key_fingerprint": "known_key", "trust_tier": "trusted", "authority_ceiling": 1.0}}
    ]
    registry = TrustRegistry(contacts)
    
    # 1. Unknown passive node -> logs telemetry, compiled
    n_passive = MockNeuralNode("pass_1", "remote", executable_code=None, key_fingerprint="unknown_1")
    # 2. Unknown executable node -> dropped
    n_exec = MockNeuralNode("exec_1", "remote", executable_code="print('hello')", key_fingerprint="unknown_1")
    # 3. Known executable node -> allowed
    n_known_exec = MockNeuralNode("exec_2", "remote", executable_code="print('hello')", key_fingerprint="known_key")
    
    with caplog.at_level(logging.INFO):
        verified = registry.verify_and_cap_nodes([n_passive, n_exec, n_known_exec])
        
    assert n_passive in verified
    assert n_exec not in verified
    assert n_known_exec in verified
    
    log_text = caplog.text
    assert "TELEMETRY: AdapterPacket activated node 'pass_1' from unknown key 'unknown_1'" in log_text
    assert "Quarantining node 'exec_1'" in log_text

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

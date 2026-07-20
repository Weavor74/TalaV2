"""
test_trust.py - Auto-generated documentation base.
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

from python.ledger.chain import LedgerChain
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

def make_valid_block(chain_instance, transactions_payloads):
    txs = []
    for payload in transactions_payloads:
        tx_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        tx_id = hashlib.sha256(tx_json.encode("utf-8")).hexdigest()
        tx = payload.copy()
        tx["tx_id"] = tx_id
        tx["signature"] = chain_instance.crypto.sign(tx_json)
        txs.append(tx)
        
    block_payload = {
        "client_id": chain_instance.client_id,
        "height": chain_instance.height + 1,
        "previous_hash": chain_instance.last_block_hash,
        "transactions": txs
    }
    block_json = json.dumps(block_payload, sort_keys=True, separators=(',', ':'))
    block_hash = hashlib.sha256(block_json.encode("utf-8")).hexdigest()
    
    block = block_payload.copy()
    block["block_hash"] = block_hash
    block["public_key"] = chain_instance.crypto.get_public_key_hex()
    block["signature"] = chain_instance.crypto.sign(block_hash)
    
    return block

def test_ledger_chain_features():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_path = Path(tmpdir) / "chain.jsonl"
        chain = LedgerChain(storage_path=str(storage_path), client_id="test-client")
        
        # 1. Base transaction
        chain.append_transaction("node1", "hash1")
        
        timestamp = datetime.now(timezone.utc).isoformat() + "Z"
        
        # Test A: Imported block with "local" pointing to missing parent (should fail)
        tx1_payload = {
            "node_id": "node2", "core_hash": "hash2", "type": "html_nn_anchor",
            "timestamp": timestamp, "client_id": "test-client",
            "parent_chaincode_ids": [
                {"id": "tala-chain:missing", "resolution": "local", "source": "local"}
            ]
        }
        b1 = make_valid_block(chain, [tx1_payload])
        res1 = chain.import_blocks([b1], mode="append_only")
        assert not res1["success"]
        assert "claims to be local but not found" in res1["error"]
        
        # Test B: Imported block with "external" (should pass)
        tx2_payload = {
            "node_id": "node3", "core_hash": "hash3", "type": "html_nn_anchor",
            "timestamp": timestamp, "client_id": "test-client",
            "parent_chaincode_ids": [
                {"id": "tala-chain:external1", "resolution": "external", "source": "ext"}
            ]
        }
        b2 = make_valid_block(chain, [tx2_payload])
        res2 = chain.import_blocks([b2], mode="append_only")
        assert res2["success"], f"Failed to import external resolution block: {res2.get('error')}"
        
        # Test C: dry_run leaves chain.jsonl byte-identical
        orig_size = storage_path.stat().st_size
        tx3_payload = {
            "node_id": "node4", "core_hash": "hash4", "type": "html_nn_anchor",
            "timestamp": timestamp, "client_id": "test-client"
        }
        b3 = make_valid_block(chain, [tx3_payload])
        res3 = chain.import_blocks([b3], mode="dry_run")
        assert res3["success"]
        assert "Dry run successful" in res3["message"]
        assert storage_path.stat().st_size == orig_size
        
        # Test D: quarantine writes only to chain.quarantine.jsonl
        res4 = chain.import_blocks([b3], mode="quarantine")
        assert res4["success"]
        assert storage_path.stat().st_size == orig_size
        quarantine_path = storage_path.with_suffix(".quarantine.jsonl")
        assert quarantine_path.exists()
        assert quarantine_path.stat().st_size > 0
        
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

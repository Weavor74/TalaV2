import os
import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from python.ledger.crypto import CryptoManager

class LedgerChain:
    def __init__(self, storage_path: str | None = None, client_id: str = "steve-desktop"):
        if not storage_path:
            storage_path = Path.home() / ".tala" / "ledger" / "chain.jsonl"
        else:
            storage_path = Path(storage_path)

        self.storage_path = storage_path
        self.client_id = client_id
        
        # Ensure directory exists
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.crypto = CryptoManager()
        self.height = 0
        self.last_block_hash = "genesis"
        
        self.trust_registry = {
            "known_keys": {}
        }
        self.witness_path = self.storage_path.with_suffix(".witnesses.jsonl")
        
        self._load_chain()
        
    def _load_chain(self):
        if not self.storage_path.exists():
            return
            
        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        block = json.loads(line)
                        self.height = block.get("height", 0)
                        self.last_block_hash = block.get("block_hash", "genesis")
                    except json.JSONDecodeError:
                        pass

    def add_trusted_key(self, public_key: str, label: str, trust_tier: str = "trusted", revoked: bool = False):
        self.trust_registry["known_keys"][public_key] = {
            "label": label,
            "trust_tier": trust_tier,
            "revoked": revoked
        }

    def append_witness(self, block_hash: str, signature: str, public_key: str):
        witness = {
            "block_hash": block_hash,
            "signature": signature,
            "public_key": public_key,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z"
        }
        with open(self.witness_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(witness, sort_keys=True) + "\n")

    def _verify_lineage(self, blocks: list) -> dict:
        local_chaincodes = set()
        existing_blocks = self.get_all_blocks()
        for b in existing_blocks + blocks:
            client = b.get("client_id")
            h = b.get("height")
            b_hash = b.get("block_hash")
            for tx in b.get("transactions", []):
                t_id = tx.get("tx_id")
                chaincode_id = f"tala-chain:{client}:{h}:{b_hash}:{t_id}"
                local_chaincodes.add(chaincode_id)

        for b in blocks:
            for tx in b.get("transactions", []):
                for parent in tx.get("parent_chaincode_ids", []):
                    if isinstance(parent, str):
                        parent_id = parent
                        resolution = "local" if parent_id in local_chaincodes else "unresolved"
                    else:
                        parent_id = parent.get("id")
                        resolution = parent.get("resolution")

                    if resolution == "local" and parent_id not in local_chaincodes:
                        return {"valid": False, "message": f"Lineage validation failed: parent {parent_id} claims to be local but not found."}
                        
                    if resolution not in ("local", "external", "unresolved", "revoked", "quarantined"):
                        return {"valid": False, "message": f"Lineage validation failed: unknown resolution {resolution} for parent {parent_id}."}

        return {"valid": True, "message": "Lineage valid"}
                        
    def append_transaction(self, node_id: str, core_hash: str, tx_type: str = "html_nn_anchor", parent_chaincode_ids: list | None = None) -> dict:
        """
        Creates a transaction, signs it, places it into a block, and appends the block.
        Returns the new block and the generated chaincode_id.
        """
        timestamp = datetime.now(timezone.utc).isoformat() + "Z"
        
        # Resolve structured parents if they are strings
        structured_parents = []
        if parent_chaincode_ids:
            local_chaincodes = set()
            for b in self.get_all_blocks():
                client = b.get("client_id")
                h = b.get("height")
                b_hash = b.get("block_hash")
                for tx in b.get("transactions", []):
                    t_id = tx.get("tx_id")
                    local_chaincodes.add(f"tala-chain:{client}:{h}:{b_hash}:{t_id}")
                    
            for p in parent_chaincode_ids:
                if isinstance(p, str):
                    structured_parents.append({
                        "id": p,
                        "resolution": "local" if p in local_chaincodes else "unresolved",
                        "source": "local-ledger" if p in local_chaincodes else "unknown"
                    })
                elif isinstance(p, dict):
                    structured_parents.append(p)

        # 1. Create Transaction
        tx_payload = {
            "node_id": node_id,
            "core_hash": core_hash,
            "type": tx_type,
            "timestamp": timestamp,
            "client_id": self.client_id
        }
        if structured_parents:
            tx_payload["parent_chaincode_ids"] = structured_parents
        
        # Serialize deterministically
        tx_json = json.dumps(tx_payload, sort_keys=True, separators=(',', ':'))
        tx_id = hashlib.sha256(tx_json.encode("utf-8")).hexdigest()
        
        tx = tx_payload.copy()
        tx["tx_id"] = tx_id
        tx["signature"] = self.crypto.sign(tx_json)
        
        # 2. Create Block
        new_height = self.height + 1
        block_payload = {
            "client_id": self.client_id,
            "height": new_height,
            "previous_hash": self.last_block_hash,
            "transactions": [tx]
        }
        
        block_json = json.dumps(block_payload, sort_keys=True, separators=(',', ':'))
        block_hash = hashlib.sha256(block_json.encode("utf-8")).hexdigest()
        
        block = block_payload.copy()
        block["block_hash"] = block_hash
        block["public_key"] = self.crypto.get_public_key_hex()
        block["signature"] = self.crypto.sign(block_hash)
        
        # 3. Append to Ledger
        with open(self.storage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(block, sort_keys=True) + "\n")
            
        # Update state
        self.height = new_height
        self.last_block_hash = block_hash
        
        # Generate chaincode_id
        # Format: tala-chain:<client_id>:<height>:<block_hash>:<tx_id>
        chaincode_id = f"tala-chain:{self.client_id}:{self.height}:{block_hash}:{tx_id}"
        
        return {
            "block": block,
            "chaincode_id": chaincode_id,
            "tx_id": tx_id
        }

    def get_all_blocks(self):
        blocks = []
        if not self.storage_path.exists():
            return blocks
        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    blocks.append(json.loads(line))
        return blocks

    def verify_chain(self) -> dict:
        """
        Verifies the entire chain sequentially.
        Checks block hashes, previous block links, block signatures, and transaction signatures.
        """
        blocks = self.get_all_blocks()
        if not blocks:
            return {"valid": True, "message": "Chain is empty"}
            
        previous_hash = "genesis"
        expected_height = 1
        
        for block in blocks:
            if block.get("height") != expected_height:
                return {"valid": False, "message": f"Block {expected_height} has invalid height {block.get('height')}"}
                
            if block.get("previous_hash") != previous_hash:
                return {"valid": False, "message": f"Block {expected_height} previous_hash mismatch"}
                
            block_payload = {
                "client_id": block["client_id"],
                "height": block["height"],
                "previous_hash": block["previous_hash"],
                "transactions": block["transactions"]
            }
            block_json = json.dumps(block_payload, sort_keys=True, separators=(',', ':'))
            computed_block_hash = hashlib.sha256(block_json.encode("utf-8")).hexdigest()
            
            if computed_block_hash != block["block_hash"]:
                return {"valid": False, "message": f"Block {expected_height} hash mismatch"}
                
            # Verify block signature
            if not CryptoManager.verify_signature(block["public_key"], computed_block_hash, block["signature"]):
                return {"valid": False, "message": f"Block {expected_height} has invalid signature"}
                
            # Verify transactions
            for tx in block["transactions"]:
                tx_payload = {k: v for k, v in tx.items() if k not in ("tx_id", "signature")}
                tx_json = json.dumps(tx_payload, sort_keys=True, separators=(',', ':'))
                computed_tx_id = hashlib.sha256(tx_json.encode("utf-8")).hexdigest()
                
                if computed_tx_id != tx["tx_id"]:
                    return {"valid": False, "message": f"Transaction {tx['tx_id']} hash mismatch in block {expected_height}"}
                    
                # Note: Currently transactions are signed by the local client. If we import blocks,
                # we don't necessarily know their public key unless it's in the block.
                # Since block contains public_key, we can assume txs are signed by the same key for now.
                if not CryptoManager.verify_signature(block["public_key"], tx_json, tx["signature"]):
                    return {"valid": False, "message": f"Transaction {tx['tx_id']} has invalid signature in block {expected_height}"}
                    
            previous_hash = computed_block_hash
            expected_height += 1
            
        return {"valid": True, "message": f"Chain is valid. Verified {len(blocks)} blocks."}

    def import_blocks(self, blocks: list, mode: str = "append_only") -> dict:
        """
        Attempts to append a bundle of blocks. Verifies them first.
        Modes: append_only, dry_run, quarantine
        """
        if not blocks:
            return {"success": True, "imported": 0}
            
        # Get current state
        current_blocks = self.get_all_blocks()
        
        # Merge and verify
        # In a real network, we'd handle forks. Here we just strictly append if they build on our tip.
        if blocks[0].get("height") != self.height + 1:
            return {"success": False, "error": f"Imported blocks start at height {blocks[0].get('height')}, expected {self.height + 1}"}
            
        if blocks[0].get("previous_hash") != self.last_block_hash:
            return {"success": False, "error": f"Imported block previous_hash doesn't match local tip"}
            
        # Write temporarily to test verification
        with open(self.storage_path, "a", encoding="utf-8") as f:
            for block in blocks:
                f.write(json.dumps(block, sort_keys=True) + "\n")
                
        verification = self.verify_chain()
        if verification["valid"]:
            lineage_verification = self._verify_lineage(blocks)
            if not lineage_verification["valid"]:
                verification = lineage_verification
                
        # Also check trust registry for revoked imported blocks
        for block in blocks:
            pub = block.get("public_key")
            if pub in self.trust_registry["known_keys"]:
                if self.trust_registry["known_keys"][pub].get("revoked"):
                    verification = {"valid": False, "message": f"Block signed by revoked key {pub}"}
                    break

        if not verification["valid"] or mode == "dry_run" or mode == "quarantine":
            # Revert
            with open(self.storage_path, "w", encoding="utf-8") as f:
                for block in current_blocks:
                    f.write(json.dumps(block, sort_keys=True) + "\n")
                    
            if mode == "quarantine":
                quarantine_path = self.storage_path.with_suffix(".quarantine.jsonl")
                with open(quarantine_path, "a", encoding="utf-8") as f:
                    for block in blocks:
                        f.write(json.dumps(block, sort_keys=True) + "\n")
                return {"success": True, "imported": 0, "quarantined": len(blocks), "message": "Blocks quarantined."}

            if not verification["valid"]:
                return {"success": False, "error": verification["message"]}
                
            if mode == "dry_run":
                return {"success": True, "imported": 0, "message": "Dry run successful"}
            
        # Success, update state
        self.height = blocks[-1]["height"]
        self.last_block_hash = blocks[-1]["block_hash"]
        return {"success": True, "imported": len(blocks)}

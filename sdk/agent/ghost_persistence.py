"""
NAME
       ghost_persistence - Offline-first thought-state persistence for agent loops

SYNOPSIS
       from sdk.agent.ghost_persistence import commit_to_ghost, restore_from_ghost

DESCRIPTION
       ghost_persistence provides serialization and recovery of the agent's
       internal thought-state, process context, and variable scopes using the
       local Ghost Blockchain node.
"""

from __future__ import annotations

import os
import json
import logging
import hashlib
import urllib.request
from pathlib import Path
from typing import Any

from sdk.agent.anchor import _load_or_create_document_key, _encrypt_document

log = logging.getLogger(__name__)

def _decrypt_document(encrypted_document: dict) -> dict:
    """
    NAME
           _decrypt_document - Decrypt AES-256-GCM encrypted document
           
    SYNOPSIS
           plaintext = _decrypt_document(encrypted_document)
           
    DESCRIPTION
           Decrypts the ciphertext payload using the local vault document key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    
    key = _load_or_create_document_key()
    aesgcm = AESGCM(key)
    
    nonce = base64.b64decode(encrypted_document["nonce"])
    ciphertext = base64.b64decode(encrypted_document["ciphertext"])
    tag = base64.b64decode(encrypted_document["tag"])
    aad = encrypted_document["aad"].encode("utf-8")
    
    ciphertext_and_tag = ciphertext + tag
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, aad)
    return json.loads(plaintext_bytes.decode("utf-8"))

async def commit_to_ghost(
    task_id: str,
    process_context: dict[str, Any],
    variable_state: dict[str, Any],
    intended_next_step_hash: str,
) -> str:
    """
    NAME
           commit_to_ghost - Commit agent thought-state to the local Ghost Blockchain
           
    SYNOPSIS
           tx_id = await commit_to_ghost(task_id, process_context, variable_state, intended_next_step_hash)
           
    DESCRIPTION
           Saves thought-state metadata to the PoA blockchain node under a
           'thought-state' document type.
    """
    from sdk.agent.anchor import canonicalize_and_anchor

    node_id = f"ghost_commit_{task_id}"
    content_html = f"<div>Thought-state snapshot for task: {task_id}</div>"
    state_data = {
        "task_id": task_id,
        "process_context": process_context,
        "variable_state": variable_state,
        "intended_next_step_hash": intended_next_step_hash,
    }

    log.info("GhostPersistence: Committing thought-state to local blockchain for task %s", task_id)
    
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        node_id=node_id,
        content_html=content_html,
        state_data=state_data,
        document_type="thought-state",
        creator="tala-agent-loop",
    )
    return chaincode_id

async def restore_from_ghost(task_id: str) -> dict[str, Any] | None:
    """
    NAME
           restore_from_ghost - Restore agent thought-state from local Ghost Blockchain
           
    SYNOPSIS
           state = await restore_from_ghost(task_id)
           
    DESCRIPTION
           Reads the local chain.json directly, finds the latest transaction
           for this task of type 'thought-state', decrypts it, and returns the state.
    """
    log.info("GhostPersistence: Attempting to restore thought-state for task %s", task_id)
    
    # Read chain.json directly (fully local and fast)
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        data_dir = Path(user_data) / "blockchain" / "data"
    else:
        # Walk up from this file to find blockchain/data
        repo_root = Path(__file__).resolve().parent.parent.parent
        data_dir = repo_root / "blockchain" / "data"
        
    chain_file = data_dir / "chain.json"
    if not chain_file.exists():
        log.warning("GhostPersistence: chain.json not found at %s", chain_file)
        return None
        
    try:
        with open(chain_file, "r", encoding="utf-8") as f:
            blocks = json.load(f)
            
        # Search backward from the latest blocks
        for block in reversed(blocks):
            for tx in block.get("transactions", []):
                if tx.get("id") == f"ghost_commit_{task_id}" and tx.get("metadata", {}).get("document_type") == "thought-state":
                    # Found it! Decrypt the document
                    doc = tx.get("document")
                    if doc:
                        decrypted = _decrypt_document(doc)
                        return decrypted.get("state_data")
    except Exception as exc:
        log.warning("GhostPersistence: Failed to restore thought-state: %s", exc)
        
    return None

def get_local_status() -> dict[str, Any]:
    """
    NAME
           get_local_status - Query local blockchain status
           
    SYNOPSIS
           status = get_local_status()
           
    DESCRIPTION
           Hits the local status endpoint.
    """
    try:
        url = "http://127.0.0.1:8332/chain/status"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}

def get_remote_status() -> dict[str, Any]:
    """
    NAME
           get_remote_status - Query remote blockchain status
           
    SYNOPSIS
           status = get_remote_status()
           
    DESCRIPTION
           Hits the remote status endpoint via gcloud ssh.
    """
    import subprocess
    try:
        cmd = [
            "gcloud", "compute", "ssh", "tala-vps",
            "--zone=us-central1-a",
            "--command=curl -s http://127.0.0.1:8332/chain/status"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
        return json.loads(res.stdout.strip())
    except Exception as exc:
        return {"error": str(exc)}

async def pre_flight_reconciliation() -> bool:
    """
    NAME
           pre_flight_reconciliation - Offline-first pre-flight block reconciliation
           
    SYNOPSIS
           success = await pre_flight_reconciliation()
           
    DESCRIPTION
           Compares local Ghost Blockchain and remote Main Blockchain status.
           If diverged, pushes local block updates to VPS and restarts services.
    """
    import asyncio
    import subprocess
    from python.harness.db import log_to_db

    log.info("GhostPersistence: Initiating pre-flight reconciliation check.")
    
    # Run status fetches in threads to keep loop non-blocking
    local_status = await asyncio.to_thread(get_local_status)
    remote_status = await asyncio.to_thread(get_remote_status)

    if "error" in local_status:
        log.warning("GhostPersistence: Local blockchain unreachable: %s", local_status["error"])
        return False
    if "error" in remote_status:
        log.warning("GhostPersistence: Remote blockchain unreachable: %s", remote_status["error"])
        return False

    local_height = local_status.get("height")
    local_hash = local_status.get("latestBlockHash")
    remote_height = remote_status.get("height")
    remote_hash = remote_status.get("latestBlockHash")

    diverged = (local_height != remote_height) or (local_hash != remote_hash)

    if diverged:
        conflict_msg = (
            f"Conflict-Event: Pre-flight state divergence. "
            f"Local hash: {local_hash} (height {local_height}), "
            f"Remote hash: {remote_hash} (height {remote_height})."
        )
        log.warning("GhostPersistence: %s", conflict_msg)

        # Log conflict warning to local DB
        try:
            await log_to_db(
                level="WARNING",
                message=conflict_msg,
                trace={
                    "event_type": "Conflict-Event",
                    "trigger": "pre_flight_reconciliation",
                    "local": {"height": local_height, "hash": local_hash},
                    "remote": {"height": remote_height, "hash": remote_hash},
                }
            )
        except Exception as db_exc:
            log.warning("GhostPersistence: Failed to log conflict to database: %s", db_exc)

        # Execute push/overwrite to restore consensus using local state
        try:
            repo_root = Path(__file__).resolve().parent.parent.parent
            local_chain_path = repo_root / "blockchain" / "data" / "chain.json"

            log.info("GhostPersistence: Overwriting remote ledger with local Ghost Blockchain...")
            cmd_scp = [
                "gcloud", "compute", "scp",
                str(local_chain_path),
                "tala-vps:chain.json",
                "--zone=us-central1-a"
            ]
            await asyncio.to_thread(subprocess.run, cmd_scp, check=True, shell=True)

            cmd_mv_restart = [
                "gcloud", "compute", "ssh", "tala-vps",
                "--zone=us-central1-a",
                "--command=sudo mv ~/chain.json /opt/tala/blockchain/data/chain.json && sudo systemctl restart tala-vps.service"
            ]
            await asyncio.to_thread(subprocess.run, cmd_mv_restart, check=True, shell=True)
            log.info("GhostPersistence: Reconciliation complete. Remote service restarted.")
            return True
        except Exception as sync_exc:
            log.error("GhostPersistence: Reconciliation push failed: %s", sync_exc)
            return False
    else:
        log.info("GhostPersistence: Pre-flight check passed. Blockchain states are synchronized.")
        return True

def check_hash() -> str:
    """
    NAME
           check_hash - Perform a lightweight consensus check
           
    SYNOPSIS
           divergence_type = check_hash()
           
    DESCRIPTION
           Compares local and remote chain heights and hashes. Returns:
           - 'none' if in sync.
           - 'minor' if heartbeat missed or unreachable.
           - 'major' if heights/hashes mismatch.
    """
    local_status = get_local_status()
    remote_status = get_remote_status()

    if "error" in local_status:
        return "minor"
    if "error" in remote_status:
        return "minor"

    local_height = local_status.get("height")
    local_hash = local_status.get("latestBlockHash")
    remote_height = remote_status.get("height")
    remote_hash = remote_status.get("latestBlockHash")

    if local_height != remote_height or local_hash != remote_hash:
        return "major"

    return "none"

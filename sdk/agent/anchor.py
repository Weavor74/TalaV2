"""
anchor.py - Auto-generated documentation base.
"""

import json
import hashlib
import urllib.request
import logging
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_CHAIN_DOCUMENT_KEY_FILE = "blockchain_document_key.bin"


def _key_path() -> Path:
    configured = os.environ.get("TALA_CHAIN_DOCUMENT_KEY_FILE")
    if configured:
        return Path(configured).expanduser().resolve()

    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        return Path(user_data).expanduser().resolve() / "vault" / ".keys" / _CHAIN_DOCUMENT_KEY_FILE

    return Path(__file__).resolve().parents[2] / "vault" / ".keys" / _CHAIN_DOCUMENT_KEY_FILE


def _load_or_create_document_key() -> bytes:
    env_key = os.environ.get("TALA_CHAIN_DOCUMENT_KEY")
    if env_key:
        key = base64.b64decode(env_key)
        if len(key) != 32:
            raise ValueError("TALA_CHAIN_DOCUMENT_KEY must decode to 32 bytes")
        return key

    path = _key_path()
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError(f"Invalid blockchain document key length at {path}")
        return key

    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _encrypt_document(document: dict, associated_data: str) -> dict:
    plaintext = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext_and_tag = AESGCM(_load_or_create_document_key()).encrypt(
        nonce,
        plaintext,
        associated_data.encode("utf-8"),
    )
    return {
        "encrypted": True,
        "algorithm": "AES-256-GCM",
        "key_id": "local-vault:blockchain-document-key:v1",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext_and_tag[:-16]).decode("ascii"),
        "tag": base64.b64encode(ciphertext_and_tag[-16:]).decode("ascii"),
        "aad": associated_data,
    }

def canonicalize_and_anchor(node_id: str, content_html: str, state_data: dict, adapter_packet: dict | None = None, creator: str = "tala-system", parent_chaincode_ids: list[str] | None = None, document_type: str = "html-nn"):
    """
    Compute a canonical hash of the core content and anchor it to the local PoA blockchain.
    """
    content_clean = "\n".join([line.strip() for line in content_html.splitlines() if line.strip()])
    state_json = json.dumps(state_data, sort_keys=True, separators=(',', ':'))
    
    core_content = f"{content_clean}\n{state_json}"
    if adapter_packet is not None:
        adapter_json = json.dumps(adapter_packet, sort_keys=True, separators=(',', ':'))
        core_content += f"\n{adapter_json}"
        
    core_hash = hashlib.sha256(core_content.encode("utf-8")).hexdigest()
    
    chaincode_id = "unanchored"
    anchoring_status = "failed"
    raw_response = None
    
    try:
        document = {
            "document_type": document_type,
            "encoding": "utf-8",
            "content_html": content_html,
            "state_data": state_data,
            "adapter_packet": adapter_packet,
        }
        encrypted_document = _encrypt_document(document, f"{node_id}:{core_hash}")
        payload = {
            "id": node_id,
            "type": "node-create",
            "hash": core_hash,
            "creator": creator,
            "counterparty": "blockchain-ledger",
            "document": encrypted_document,
            "metadata": {
                "storage_policy": "local-blockchain-encrypted-document",
                "content_hash_algorithm": "sha256",
                "document_type": document_type,
                "encryption": "AES-256-GCM",
            },
        }
        if parent_chaincode_ids:
            payload["parent_chaincode_ids"] = parent_chaincode_ids
            
        tx_data = json.dumps(payload).encode("utf-8")
        
        req = urllib.request.Request(
            "http://127.0.0.1:8332/chain/tx",
            data=tx_data,
            headers={
                "Content-Type": "application/json",
                "Content-Length": len(tx_data)
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            response_body = response.read()
            raw_response = response_body.decode("utf-8", errors="ignore")
            try:
                tx_res = json.loads(response_body)
                for key in ("tx_id", "transaction_id", "hash", "block_hash", "id"):
                    if key in tx_res and tx_res[key]:
                        chaincode_id = tx_res[key]
                        anchoring_status = "anchored"
                        break
            except Exception:
                pass
    except Exception as exc:
        raw_response = str(exc)
        logger.debug("Failed to anchor node '%s' on blockchain: %s", node_id, exc)
        
    public_key = None
    try:
        if raw_response and anchoring_status == "anchored":
            tx_res = json.loads(raw_response)
            if "block" in tx_res and "public_key" in tx_res["block"]:
                public_key = tx_res["block"]["public_key"]
    except Exception:
        pass
        
    return core_hash, chaincode_id, anchoring_status, raw_response, public_key

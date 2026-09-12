"""
NAME
       diagnose_html_nn - Diagnostic audit of the HTML-NN serialization pipeline

SYNOPSIS
       uv run python scripts/diagnose_html_nn.py

DESCRIPTION
       Runs a diagnostic check on the last 3 generated html-nn files in the
       nodes directory. Verifies Base64 LoRA weights, metadata headers, PoU
       citations, and performs a dry-run safetensors decode. Records the audit
       directly into the local Ghost Blockchain.
"""

from __future__ import annotations

import sys
import json
import base64
import hashlib
import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

# Ensure we can import python.harness and sdk.agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def get_nodes_dir() -> Path:
    """Return the absolute path to the nodes/ directory."""
    return Path(__file__).resolve().parent.parent / "nodes"

async def audit_file(file_path: Path) -> dict[str, Any]:
    """
    NAME
           audit_file - Audit a single html-nn file
           
    SYNOPSIS
           result = await audit_file(file_path)
           
    DESCRIPTION
           Checks the file for Base64 weights, Target_Layer_Schema, Rank,
           Base_Model_Hash, and Usage_Citation.
    """
    print(f"\n[Audit] Auditing file: {file_path.name}")
    content = file_path.read_text(encoding="utf-8")
    
    # Check 1: Serialization Integrity (Base64 weights A and B)
    # Looking for a script or tag with id "lora-weights" or similar
    weights_tag = "lora-weights"
    weights_found = weights_tag in content
    weights_valid = False
    weights_data = None
    
    if weights_found:
        # Simple regex/find extraction of base64 content
        try:
            import re
            m = re.search(r'<script[^>]*id="lora-weights"[^>]*>(.*?)</script>', content, re.DOTALL)
            if m:
                base64_str = m.group(1).strip()
                weights_data = base64.b64decode(base64_str)
                weights_valid = len(weights_data) > 0
        except Exception:
            pass

    # Check 2: Metadata Header
    # Target_Layer_Schema, Rank (r), Base_Model_Hash
    has_target_layer = "Target_Layer_Schema" in content
    has_rank = "Rank" in content or "data-rank" in content or '"r"' in content
    has_base_hash = "Base_Model_Hash" in content or "data-base-model-hash" in content
    
    if weights_valid and weights_data:
        try:
            header_size = int.from_bytes(weights_data[:8], "little")
            header_json = weights_data[8:8+header_size].decode("utf-8")
            header = json.loads(header_json)
            metadata = header.get("__metadata__", {})
            if "Target_Layer_Schema" in metadata:
                has_target_layer = True
            if "Rank" in metadata:
                has_rank = True
            if "Base_Model_Hash" in metadata:
                has_base_hash = True
        except Exception:
            pass
    
    # Extract state json to inspect fields
    state_data = {}
    try:
        import re
        state_m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', content, re.DOTALL)
        if state_m:
            state_data = json.loads(state_m.group(1).strip())
    except Exception:
        pass
        
    # Check 3: PoU Readiness (Usage_Citation in Ghost Ledger)
    # Search local chain.json for Usage_Citation associated with this node id or hash
    pou_ready = False
    repo_root = Path(__file__).resolve().parent.parent
    chain_file = repo_root / "blockchain" / "data" / "chain.json"
    if chain_file.exists():
        try:
            with open(chain_file, "r", encoding="utf-8") as f:
                blocks = json.load(f)
            for block in blocks:
                for tx in block.get("transactions", []):
                    # Look for usage citations targeting this node_id
                    if tx.get("type") == "usage-citation" and tx.get("node_id") == file_path.stem:
                        pou_ready = True
                        break
        except Exception:
            pass

    # Check 4: Dry-run decode into safetensors in-memory
    decode_success = False
    decode_error = None
    if weights_valid and weights_data:
        try:
            # Pure Python Safetensors Decoder
            header_size = int.from_bytes(weights_data[:8], "little")
            header_json = weights_data[8:8+header_size].decode("utf-8")
            header = json.loads(header_json)
            
            # Verify keys and structural schema
            if "lora_A" in header and "lora_B" in header:
                metadata = header.get("__metadata__", {})
                if metadata.get("Target_Layer_Schema") and metadata.get("Rank") and metadata.get("Base_Model_Hash"):
                    decode_success = True
                else:
                    decode_error = f"Metadata missing expected keys. Header: {header_json}"
            else:
                decode_error = f"Header missing lora_A/lora_B. Header: {header_json}"
        except Exception as exc:
            decode_error = f"Safetensors parsing failed: {exc}"
    else:
        decode_error = "No valid Base64 weights found to decode."

    return {
        "file_name": file_path.name,
        "serialization_integrity": {
            "weights_found": weights_found,
            "weights_valid": weights_valid
        },
        "metadata_header": {
            "has_target_layer": has_target_layer,
            "has_rank": has_rank,
            "has_base_hash": has_base_hash
        },
        "pou_readiness": {
            "pou_ready": pou_ready
        },
        "self_validation": {
            "decode_success": decode_success,
            "error": decode_error
        }
    }

async def run_diagnostic_audit() -> None:
    """
    NAME
           run_diagnostic_audit - Main audit coordinator
           
    SYNOPSIS
           await run_diagnostic_audit()
           
    DESCRIPTION
           Finds the last 3 html-nn files, audits each, calculates score, and
           anchors an audit entry to the Ghost Blockchain.
    """
    nodes_dir = get_nodes_dir()
    html_files = list(nodes_dir.glob("*.html"))
    # Sort by modification time descending
    html_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    
    target_files = html_files[:3]
    if not target_files:
        print("[Audit] No html-nn files found to audit.")
        return
        
    results = []
    total_score = 0.0
    
    for f in target_files:
        res = await audit_file(f)
        results.append(res)
        
        # Calculate a self-validation score out of 100 for this node
        score = 0.0
        if res["serialization_integrity"]["weights_found"]:
            score += 25.0
        if res["serialization_integrity"]["weights_valid"]:
            score += 25.0
        if res["metadata_header"]["has_target_layer"] and res["metadata_header"]["has_rank"] and res["metadata_header"]["has_base_hash"]:
            score += 25.0
        if res["self_validation"]["decode_success"]:
            score += 25.0
        total_score += score
        print(f"  > Node Score: {score}/100.0")

    avg_score = total_score / len(target_files)
    print(f"\n[Audit] Diagnostic Audit Complete. Avg Self-Validation Score: {avg_score}%")
    
    # Prepare local blockchain audit entry to satisfy constraint:
    # "For every action taken within this session, create an audit entry in the local blockchain..."
    from sdk.agent.anchor import canonicalize_and_anchor
    
    result_hash = hashlib.sha256(json.dumps(results, sort_keys=True).encode("utf-8")).hexdigest()
    
    audit_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_id": "diagnose_html_nn",
        "input_parameters": {
            "target_files": [f.name for f in target_files]
        },
        "result_hash": result_hash,
        "self_validation_score": avg_score,
        "audit_details": results
    }
    
    node_id = f"audit_run_{int(datetime.now(UTC).timestamp())}"
    content_html = f"<div>HTML-NN Diagnostic Audit Log: {datetime.now(UTC).isoformat()}</div>"
    
    print("[Audit] Writing audit entry to the local Ghost Blockchain...")
    try:
        core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
            node_id=node_id,
            content_html=content_html,
            state_data=audit_data,
            document_type="audit-entry",
            creator="tala-diagnostic-auditor"
        )
        # canonicalize_and_anchor swallows its own failures and reports them
        # through anchoring_status rather than raising -- so a bare "success"
        # print here claimed the audit was on-chain while chaincode_id was
        # still the "unanchored" failure sentinel.
        if anchoring_status == "anchored" and chaincode_id != "unanchored":
            print(f"[Audit] Audit anchored successfully. Chaincode ID: {chaincode_id}")
        else:
            print(
                f"[Audit] WARNING: audit was NOT anchored "
                f"(status={anchoring_status}, chaincode_id={chaincode_id}). "
                f"Local content hash: {core_hash[:16]}..."
            )
    except Exception as anchor_exc:
        print(f"[Warning] Failed to anchor audit to blockchain: {anchor_exc}")

async def main():
    from python.harness.db import init_pool, close_pool
    await init_pool()
    try:
        await run_diagnostic_audit()
    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(main())

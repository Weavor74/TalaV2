"""
NAME
       diagnose_metadata_drift - Deep-scan nodes and audit/correct JSON-LD metadata drift

SYNOPSIS
       uv run python scripts/diagnose_metadata_drift.py

DESCRIPTION
       Parses the JSON-LD metadata headers of all html-nn files in nodes/.
       Detects drift or missing required keys (semantic_tags, emotional_valence,
       relevance_weight, usage_count). Triggers self-correction via update_manifest
       to repair nodes. Anchors an audit entry to the local Ghost Blockchain.
"""

from __future__ import annotations

import sys
import os
import json
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

async def audit_node_metadata(file_path: Path) -> dict[str, Any] | None:
    """
    NAME
           audit_node_metadata - Audit metadata fields of a single node
           
    SYNOPSIS
           mismatches = await audit_node_metadata(file_path)
           
    DESCRIPTION
           Parses <script id="node-metadata">. Returns dict of missing/invalid
           fields, or None if fully valid.
    """
    content = file_path.read_text(encoding="utf-8")
    
    # 1. Parse JSON-LD metadata block
    import re
    metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
    if not metadata_m:
        return {
            "node_id": file_path.stem,
            "missing_block": True,
            "missing_fields": ["semantic_tags", "emotional_valence", "relevance_weight", "usage_count"]
        }
        
    try:
        data = json.loads(metadata_m.group(1).strip())
    except Exception as exc:
        return {
            "node_id": file_path.stem,
            "corrupt_json": str(exc),
            "missing_fields": ["semantic_tags", "emotional_valence", "relevance_weight", "usage_count"]
        }
        
    missing = []
    
    # Check semantic_tags
    tags = data.get("semantic_tags")
    if tags is None or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        missing.append("semantic_tags")
        
    # Check emotional_valence
    valence = data.get("emotional_valence")
    required_valence_keys = {"joy", "caution", "curiosity", "neutral"}
    if (valence is None or not isinstance(valence, dict) or 
            not all(k in valence for k in required_valence_keys) or
            not all(isinstance(valence[k], (int, float)) for k in required_valence_keys)):
        missing.append("emotional_valence")
        
    # Check relevance_weight
    relevance = data.get("relevance_weight")
    if relevance is None or not isinstance(relevance, (int, float)) or not (0.0 <= relevance <= 1.0):
        missing.append("relevance_weight")
        
    # Check usage_count
    usage = data.get("usage_count")
    if usage is None or not isinstance(usage, int) or isinstance(usage, bool):
        missing.append("usage_count")
        
    if missing:
        return {
            "node_id": file_path.stem,
            "missing_block": False,
            "missing_fields": missing
        }
        
    return None

async def run_metadata_audit() -> None:
    """
    NAME
           run_metadata_audit - Audit all nodes and trigger self-correction
           
    SYNOPSIS
           await run_metadata_audit()
           
    DESCRIPTION
           Scans nodes, flags metadata drift, calls update_manifest to self-correct,
           and anchors a complete audit trail node to the Ghost Blockchain.
    """
    print("[Metadata-Drift] Initiating Deep-Scan of nodes directory...")
    nodes_dir = get_nodes_dir()
    html_files = list(nodes_dir.glob("*.html"))
    
    drift_report = []
    repaired_count = 0
    total_audited = len(html_files)
    
    # 1. Audit Phase
    for f in html_files:
        mismatches = await audit_node_metadata(f)
        if mismatches:
            drift_report.append(mismatches)
            
    # 2. Print Report
    print(f"\n=== METADATA-DRIFT AUDIT REPORT ===")
    print(f"Audited: {total_audited} files. Drift Detected: {len(drift_report)} files.")
    for entry in drift_report:
        print(f"File: {entry['node_id']}.html - Missing/Invalid fields: {entry['missing_fields']}")
    print("===================================\n")
    
    # 3. Self-Correction Phase
    if drift_report:
        print("[Self-Correction] Triggering Update-Manifest tool for drifted nodes...")
        from sdk.agent.tools import update_manifest
        for entry in drift_report:
            f_name = f"{entry['node_id']}.html"
            f_path = nodes_dir / f_name
            try:
                res = await update_manifest(str(f_path))
                print(f"  [Repaired] {res}")
                repaired_count += 1
            except Exception as e:
                print(f"  [Error] Failed to repair {f_name}: {e}")
                
    # 4. Calculate score and result hash
    success_rate = 1.0 if total_audited == 0 else (1.0 - (len(drift_report) - repaired_count) / total_audited)
    validation_score = success_rate * 100.0
    print(f"\n[Metadata-Drift] Self-Correction Complete. Final Validation Score: {validation_score:.1f}%")
    
    # 5. Local Blockchain Audit Entry
    from sdk.agent.anchor import canonicalize_and_anchor
    
    results_payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_id": "diagnose_metadata_drift",
        "input_parameters": {
            "total_nodes": total_audited,
        },
        "result_hash": hashlib.sha256(json.dumps(drift_report, sort_keys=True).encode("utf-8")).hexdigest(),
        "self_validation_score": validation_score,
        "drifted_count": len(drift_report),
        "repaired_count": repaired_count,
        "drift_report": drift_report
    }
    
    node_id = f"metadata_drift_audit_{int(datetime.now(UTC).timestamp())}"
    content_html = f"<div>Metadata-Drift Diagnostic Audit Log: {datetime.now(UTC).isoformat()}</div>"
    
    print("[Metadata-Drift] Writing audit entry to the local Ghost Blockchain...")
    try:
        core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
            node_id=node_id,
            content_html=content_html,
            state_data=results_payload,
            document_type="audit-entry",
            creator="tala-metadata-auditor"
        )
        print(f"[Metadata-Drift] Audit anchored successfully. Chaincode ID: {chaincode_id}")
    except Exception as anchor_exc:
        print(f"[Warning] Failed to anchor audit to blockchain: {anchor_exc}")

async def main():
    from python.harness.db import init_pool, close_pool
    await init_pool()
    try:
        await run_metadata_audit()
    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(main())

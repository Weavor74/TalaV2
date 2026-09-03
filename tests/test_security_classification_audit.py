import pytest
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from sdk.agent.tools import (
    classify_field_heuristically,
    run_security_classification_audit,
    approve_security_classification_mapping,
)

def test_classify_field_heuristically():
    # Public
    assert classify_field_heuristically("tone") == "PUBLIC"
    assert classify_field_heuristically("rp_class") == "PUBLIC"
    assert classify_field_heuristically("name") == "PUBLIC"
    
    # Secured
    assert classify_field_heuristically("pii_email") == "SECURED"
    assert classify_field_heuristically("Passport Number") == "SECURED"
    assert classify_field_heuristically("birthdate") == "SECURED"
    
    # Ambiguous -> UNCATEGORIZED
    assert classify_field_heuristically("Passport Issue Date") == "UNCATEGORIZED"


@pytest.mark.asyncio
async def test_run_security_classification_audit_ambiguity():
    fields = ["name", "tone", "Passport Number", "Passport Issue Date"]
    res = await run_security_classification_audit(fields)
    
    assert res["status"] == "MANUAL_CLASSIFICATION_REQUIRED"
    assert "Passport Issue Date" in res["ambiguous_fields"]
    assert res["mapping"]["Passport Issue Date"] == "UNCATEGORIZED"


@pytest.mark.asyncio
async def test_approve_and_lock_classification():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        chain_dir = tmp_path / "blockchain" / "data"
        chain_dir.mkdir(parents=True)
        chain_file = chain_dir / "chain.json"
        chain_file.write_text("[]", encoding="utf-8")
        
        # Manually classified map (Passport Issue Date mapped to SECURED)
        approved_map = {
            "name": "PUBLIC",
            "tone": "PUBLIC",
            "Passport Number": "SECURED",
            "Passport Issue Date": "SECURED"
        }
        
        def mock_anchor(node_id, content_html, state_data, creator, document_type, **kwargs):
            # Assert correct ledger payload format
            assert document_type == "security-configuration"
            assert state_data["event_type"] == "Security-Configuration-Event"
            assert state_data["mapping"]["Passport Issue Date"] == "SECURED"
            return "mock_hash", "mock_chaincode", "anchored", "{}", None
            
        with patch("sdk.agent.anchor.canonicalize_and_anchor", side_effect=mock_anchor), \
             patch.dict(os.environ, {"TALA_USER_DATA_ROOT": tmpdir}):
             
            # Approve mapping
            approve_res = await approve_security_classification_mapping(approved_map)
            assert "Successfully persistently locked" in approve_res
            
            # Verify config mapping file was written
            mapping_file = tmp_path / "data" / "security_mapping.json"
            assert mapping_file.exists()
            written_map = json.loads(mapping_file.read_text(encoding="utf-8"))
            assert written_map["Passport Issue Date"] == "SECURED"

            # The classification, the persisted mapping and the ledger anchor
            # are the real behaviour here and are asserted above.
            #
            # This test used to end by reading the field back through
            # get_user_data to prove the mapping took effect. That tool has
            # been deleted: it served a hardcoded dict of invented personal
            # data, so the read-back proved only that a constant was still a
            # constant.
            #
            # NOTE: security_mapping.json currently has no reader. get_user_data
            # was the only one. The classifier still writes and anchors it.

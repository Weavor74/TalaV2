import pytest
import os
import json
import tempfile
import re
from pathlib import Path
from unittest.mock import patch

from sdk.agent.tools import get_user_data, compile_lora_adapter, SecurityError

@pytest.mark.asyncio
async def test_get_user_data_public():
    # Public field should return instantly
    res = await get_user_data("name")
    assert res["status"] == "SUCCESS"
    assert res["access_level"] == "PUBLIC"
    assert res["value"] == "Steve"


@pytest.mark.asyncio
async def test_get_user_data_secured_denied():
    # Mock anchor to write to a temp ledger
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        chain_dir = tmp_path / "blockchain" / "data"
        chain_dir.mkdir(parents=True)
        chain_file = chain_dir / "chain.json"
        chain_file.write_text("[]", encoding="utf-8")
        
        def mock_anchor(node_id, content_html, state_data, creator, document_type, **kwargs):
            # Just verify that it is capturing the breach event correctly
            assert state_data["status"] == "ACCESS_DENIED"
            assert state_data["field_accessed"] == "Passport Number"
            assert document_type == "security-alert"
            return "mock_hash", "mock_chaincode", "anchored", "{}", None
            
        with patch("sdk.agent.anchor.canonicalize_and_anchor", side_effect=mock_anchor):
            res = await get_user_data("Passport Number", permission_token=None)
            assert res["status"] == "ACCESS_DENIED"
            assert res["access_level"] == "SECURED"
            assert "Permission Token required" in res["message"]


@pytest.mark.asyncio
async def test_get_user_data_secured_approved():
    res = await get_user_data("Passport Number", permission_token="MANUAL_OK_2026")
    assert res["status"] == "SUCCESS"
    assert res["access_level"] == "SECURED"
    assert res["value"] == "USA-987654321"


@pytest.mark.asyncio
async def test_pi_sanitizer_zero_leak():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        node_file = nodes_dir / "skill-test_pii.html"
        
        # HTML-NN containing SECURED access level
        node_file.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-test_pii">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["pii"],
    "access_level": "SECURED"
  }
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        # Running compile_lora_adapter should raise SecurityError immediately
        with pytest.raises(SecurityError) as excinfo:
            await compile_lora_adapter(str(node_file))
            
        assert "Attempted to encode SECURED PII" in str(excinfo.value)

import pytest
import os
import json
import tempfile
import hashlib
import hmac
from pathlib import Path
from unittest.mock import patch

from vps_registry.registry import NodeRegistry
from sdk.agent.tools import broadcast_update
from python.harness.server import handle_vps_version_push

@pytest.mark.asyncio
async def test_node_registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = NodeRegistry(tmpdir)
        node_hash = "abcdef1234567890"
        
        # Initial is empty
        assert registry.get_subscribers(node_hash) == []
        
        # Add subscriber
        registry.add_subscriber(node_hash, "agent-1")
        assert registry.get_subscribers(node_hash) == ["agent-1"]
        
        # Avoid duplicate
        registry.add_subscriber(node_hash, "agent-1")
        assert registry.get_subscribers(node_hash) == ["agent-1"]
        
        # Add second subscriber
        registry.add_subscriber(node_hash, "agent-2")
        assert registry.get_subscribers(node_hash) == ["agent-1", "agent-2"]
        
        # Persistence reload check
        registry2 = NodeRegistry(tmpdir)
        assert registry2.get_subscribers(node_hash) == ["agent-1", "agent-2"]


@pytest.mark.asyncio
async def test_broadcast_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create temp node file
        node_file = tmp_path / "skill-test.html"
        node_file.write_text("dummy content", encoding="utf-8")
        
        # Mock anchor
        def mock_anchor(node_id, content_html, state_data, creator, document_type, **kwargs):
            assert document_type == "broadcast-event"
            assert state_data["event_type"] == "Broadcast-Initiated"
            return "mock_hash", "mock_chaincode", "anchored", "{}", None
            
        with patch("sdk.agent.anchor.canonicalize_and_anchor", side_effect=mock_anchor), \
             patch.dict(os.environ, {"TALA_USER_DATA_ROOT": tmpdir}):
             
            # Initialize registry with subscriber mapping
            node_hash = hashlib.sha256(b"dummy content").hexdigest()
            registry = NodeRegistry(tmpdir)
            registry.add_subscriber(node_hash, "test-agent-alpha")
            
            res = await broadcast_update(str(node_file))
            assert res["status"] == "SUCCESS"
            assert "Broadcast-Initiated event successfully anchored" in res["message"]
            assert res["node_hash"] == node_hash
            assert res["subscribers"] == ["test-agent-alpha"]
            assert res["version_push_packet"]["signal"] == "VERSION_PUSH"
            assert "signature" in res["version_push_packet"]


@pytest.mark.asyncio
async def test_handle_vps_version_push_valid():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)
        
        content = """<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-push_valid">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["smoke"],
    "emotional_valence": {"joy": 1.0},
    "relevance_weight": 0.9,
    "usage_count": 0
  }
  </script>
  <script type="application/octet-stream" id="lora-weights">
  AAAAAAAAAABBBBBBBBBBCCCCCCCCCCDDDDDDDDDD
  </script>
</article>
</body>
</html>"""
        
        node_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        shared_secret = b"vps_shared_key_2026"
        sig = hmac.new(shared_secret, node_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        
        params = {
            "node_hash": node_hash,
            "signature": sig,
            "content_html": content
        }
        
        with patch.dict(os.environ, {"TALA_USER_DATA": tmpdir}):
            res_str = await handle_vps_version_push(params, req_id=42)
            res = json.loads(res_str)
            
            assert "error" not in res
            assert res["result"]["status"] == "SUCCESS"
            
            # Check file was written to nodes/ directory
            written_file = nodes_dir / "skill-push_valid.html"
            assert written_file.exists()
            assert "skill-push_valid" in written_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_handle_vps_version_push_invalid_signature():
    content = "dummy html"
    node_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    params = {
        "node_hash": node_hash,
        "signature": "wrong_signature",
        "content_html": content
    }
    res_str = await handle_vps_version_push(params, req_id=42)
    res = json.loads(res_str)
    assert "error" in res
    assert "Invalid digital signature" in res["error"]["message"]


@pytest.mark.asyncio
async def test_handle_vps_version_push_invalid_validation():
    content = "<html>no metadata, no weights</html>"
    node_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    shared_secret = b"vps_shared_key_2026"
    sig = hmac.new(shared_secret, node_hash.encode("utf-8"), hashlib.sha256).hexdigest()
    
    params = {
        "node_hash": node_hash,
        "signature": sig,
        "content_html": content
    }
    res_str = await handle_vps_version_push(params, req_id=42)
    res = json.loads(res_str)
    assert "error" in res
    assert "Validation failed" in res["error"]["message"]

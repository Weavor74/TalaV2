import pytest
import os
import json
import tempfile
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

from sdk.agent.goal_engine import GoalEngine

@pytest.mark.asyncio
async def test_check_dream_trigger():
    engine = GoalEngine()
    engine.active_adapter_buffer = "some_adapter"
    
    # 1. active_adapter_buffer is set -> trigger should be False
    assert await engine.check_dream_trigger() is False
    
    engine.active_adapter_buffer = None
    # 2. Mocking DB status to show active tasks
    with patch("python.harness.db.get_pool") as mock_pool, \
         patch("python.harness.db.fetch") as mock_fetch:
        mock_pool.return_value = MagicMock()
        mock_fetch.return_value = [{"id": "task-1"}]
        
        assert await engine.check_dream_trigger() is False


@pytest.mark.asyncio
async def test_dream_cycle_consolidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        node_file = nodes_dir / "skill-test_dream.html"
        node_file.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-test_dream">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["sync"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.9,
      "curiosity": 0.1,
      "neutral": 0.1
    },
    "relevance_weight": 1.0,
    "usage_count": 5
  }
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        # Mock the chain.json to simulate past learning/non-critical events
        chain_dir = tmp_path / "blockchain" / "data"
        chain_dir.mkdir(parents=True)
        chain_file = chain_dir / "chain.json"
        
        mock_txs = [
            {
                "type": "behavioral-learning",
                "document": {
                    "encrypted": False,
                    "state_data": {
                        "node_id": "skill-test_dream",
                        "feedback_critique": "The caution is too high, please reduce safety caution."
                    }
                }
            }
        ]
        
        mock_blocks = [
            {
                "index": 1,
                "transactions": mock_txs
            }
        ]
        
        chain_file.write_text(json.dumps(mock_blocks, indent=2), encoding="utf-8")
        
        # Patch canonicalize_and_anchor to write directly to our temp chain.json
        def mock_anchor(node_id, content_html, state_data, creator, document_type, **kwargs):
            # Read existing
            with open(chain_file, "r", encoding="utf-8") as f:
                blocks = json.load(f)
            
            new_tx = {
                "type": document_type,
                "node_id": node_id,
                "state_data": state_data
            }
            blocks[-1]["transactions"].append(new_tx)
            
            with open(chain_file, "w", encoding="utf-8") as f:
                json.dump(blocks, f, indent=2)
                
            return "mock_hash", "mock_chaincode", "anchored", "{}", None

        with patch("sdk.agent.anchor.canonicalize_and_anchor", side_effect=mock_anchor):
            # Instantiate GoalEngine and run dream_cycle
            engine = GoalEngine(root_dir=tmpdir)
            summary = await engine.dream_cycle()
            
            assert "Refactored 1 nodes" in summary
            assert "skill-test_dream" in summary
            
            # Verify the file was modified and caution was reduced
            content = node_file.read_text(encoding="utf-8")
            metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
            assert metadata_m is not None
            
            data = json.loads(metadata_m.group(1).strip())
            assert data["emotional_valence"]["caution"] == 0.7  # 0.9 - 0.2
            assert 'id="lora-weights"' in content
            
            # Verify that the dream event is now appended to chain.json
            with open(chain_file, "r", encoding="utf-8") as f:
                blocks = json.load(f)
                
            last_block = blocks[-1]
            dream_tx = next((tx for tx in last_block["transactions"] if tx.get("type") == "dream-log"), None)
            assert dream_tx is not None
            assert "skill-test_dream" in dream_tx["state_data"]["summary"]

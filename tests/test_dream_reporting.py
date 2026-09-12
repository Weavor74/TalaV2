import pytest
import json
import tempfile
import re
from pathlib import Path
from unittest.mock import patch

from sdk.agent.goal_engine import GoalEngine

def test_dream_report_filter():
    engine = GoalEngine()
    
    # 1. No changes -> False
    assert engine.dream_report_filter([]) is False
    
    # 2. Standard node, low risk delta -> False
    assert engine.dream_report_filter([{"node_id": "skill-nav", "node_type": "Standard", "risk_delta": 0.2}]) is False
    
    # 3. Core node -> True
    assert engine.dream_report_filter([{"node_id": "system-nav", "node_type": "Core", "risk_delta": 0.1}]) is True
    
    # 4. Standard node, high risk delta -> True
    assert engine.dream_report_filter([{"node_id": "skill-nav", "node_type": "Standard", "risk_delta": 0.7}]) is True


def test_generate_dream_journal_entry():
    engine = GoalEngine()
    changes = [{"node_id": "skill-nav", "node_type": "Standard", "risk_delta": 0.2, "shifts": {"caution": "0.80 -> 0.60"}}]
    
    entry = engine.generate_dream_journal_entry(changes, "Consolidated", "Calibrated caution.")
    assert "Status: Consolidated." in entry
    assert "Decision: [SILENT]" in entry
    assert "Calibrated caution." in entry
    assert "skill-nav (caution: 0.80 -> 0.60)" in entry
    assert "Circuit-Breaker" in entry


@pytest.mark.asyncio
async def test_lucid_dreaming_rollback_and_tiers():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        
        # 1. A Core safety node (should trigger rollback when caution drops < 0.3)
        core_node = nodes_dir / "system-safety.html"
        core_node.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="system-safety">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["safety"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.4,
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

        # 2. A Standard node (should optimize successfully without rollback)
        standard_node = nodes_dir / "skill-nav.html"
        standard_node.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-nav">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["navigation"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.8,
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

        # Mock the chain.json to simulate feedback critiques for both nodes
        chain_dir = tmp_path / "blockchain" / "data"
        chain_dir.mkdir(parents=True)
        chain_file = chain_dir / "chain.json"
        
        mock_txs = [
            {
                "type": "behavioral-learning",
                "document": {
                    "encrypted": False,
                    "state_data": {
                        "node_id": "system-safety",
                        "feedback_critique": "Too cautious, reduce safety checks."
                    }
                }
            },
            {
                "type": "behavioral-learning",
                "document": {
                    "encrypted": False,
                    "state_data": {
                        "node_id": "skill-nav",
                        "feedback_critique": "Too cautious, speed up navigation."
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
            engine = GoalEngine(root_dir=tmpdir)
            summary = await engine.dream_cycle()
            
            # The standard node is optimized (Refactored 1 nodes: skill-nav)
            # The core node triggered a rollback and was NOT refactored
            assert "Refactored 1 nodes" in summary
            assert "skill-nav" in summary
            assert "system-safety" not in summary
            
            # Verify system-safety was reverted to caution = 0.4
            content = core_node.read_text(encoding="utf-8")
            metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
            assert metadata_m is not None
            data = json.loads(metadata_m.group(1).strip())
            assert data["emotional_valence"]["caution"] == 0.4
            
            # Verify skill-nav was updated to caution = 0.6
            content_nav = standard_node.read_text(encoding="utf-8")
            metadata_nav_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content_nav, re.DOTALL)
            assert metadata_nav_m is not None
            data_nav = json.loads(metadata_nav_m.group(1).strip())
            assert data_nav["emotional_valence"]["caution"] == 0.6
            
            # Verify that the dream-rollback event is in the chain.json
            with open(chain_file, "r", encoding="utf-8") as f:
                blocks = json.load(f)
                
            last_block = blocks[-1]
            rollback_tx = next((tx for tx in last_block["transactions"] if tx.get("type") == "dream-rollback"), None)
            assert rollback_tx is not None
            assert "safety" in rollback_tx["state_data"]["break_reason"]

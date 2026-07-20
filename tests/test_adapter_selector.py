import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from sdk.agent.goal_engine import GoalEngine, AdapterSelector, infer_emotional_state

def test_infer_emotional_state():
    # Curiosity
    s1 = infer_emotional_state("How do I sync the blockchain?")
    assert s1["curiosity"] == 0.9
    assert s1["caution"] == 0.1
    
    # Caution
    s2 = infer_emotional_state("Verify the safety and security gating checks.")
    assert s2["caution"] == 0.9
    assert s2["curiosity"] == 0.1
    
    # Neutral
    s3 = infer_emotional_state("Perform a simple run task.")
    assert s3["neutral"] == 0.9
    assert s3["joy"] == 0.1


@pytest.mark.asyncio
async def test_adapter_selector_ranking():
    # Create temp nodes directory with 2 dummy HTML-NN files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Node 1: safety node (caution oriented)
        node1_path = tmp_path / "skill-safety_check.html"
        node1_path.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-safety_check">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["safety-gate", "governance", "security"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.9,
      "curiosity": 0.1,
      "neutral": 0.3
    },
    "relevance_weight": 1.0,
    "usage_count": 9
  }
  </script>
  <script type="application/octet-stream" id="lora-weights">
  BASE64WEIGHTS_SAFETY
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        # Node 2: blockchain sync node (curiosity oriented)
        node2_path = tmp_path / "skill-blockchain_sync.html"
        node2_path.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-blockchain_sync">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["blockchain", "ledger", "sync"],
    "emotional_valence": {
      "joy": 0.4,
      "caution": 0.2,
      "curiosity": 0.8,
      "neutral": 0.2
    },
    "relevance_weight": 1.0,
    "usage_count": 1
  }
  </script>
  <script type="application/octet-stream" id="lora-weights">
  BASE64WEIGHTS_SYNC
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        selector = AdapterSelector(str(tmp_path))
        
        # 1. Query matching Node 2 (curiosity + sync words)
        best_sync = selector.select_adapter("How do I sync the blockchain?")
        assert best_sync is not None
        assert best_sync["node_id"] == "skill-blockchain_sync"
        assert best_sync["payload"] == "BASE64WEIGHTS_SYNC"
        
        # 2. Query matching Node 1 (caution + safety words)
        best_safety = selector.select_adapter("Secure the vault and verify safety gating.")
        assert best_safety is not None
        assert best_safety["node_id"] == "skill-safety_check"
        assert best_safety["payload"] == "BASE64WEIGHTS_SAFETY"


@pytest.mark.asyncio
@patch("sdk.agent.queue.GoalTreeDecomposer.decompose")
@patch("sdk.agent.queue.BatchQueue.enqueue_task")
async def test_goal_engine_adapter_routing(mock_enqueue, mock_decompose):
    # Mock decomposition to return empty tasks to prevent actual execution
    mock_decompose.return_value = []
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        node_path = nodes_dir / "skill-sync.html"
        node_path.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-sync">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["sync", "blockchain"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.1,
      "curiosity": 0.9,
      "neutral": 0.1
    },
    "relevance_weight": 1.0,
    "usage_count": 0
  }
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        engine = GoalEngine(root_dir=tmpdir)
        res = await engine.run_goal("Sync blockchain now")
        
        # Verify the router selected the adapter and set the buffer
        assert engine.active_adapter_buffer is not None
        assert engine.active_adapter_buffer["node_id"] == "skill-sync"

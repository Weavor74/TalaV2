import pytest
import os
import json
import tempfile
import re
from pathlib import Path

from sdk.agent.goal_engine import GoalEngine
from sdk.agent.tools import adjust_behavioral_weights

@pytest.mark.asyncio
async def test_should_i_adjust_gate():
    engine = GoalEngine()
    
    # 1. Low severity, non-recurring -> should be False
    assert engine.should_i_adjust("It was okay", usage_count=1, is_recurring=False) is False
    
    # 2. High severity ("bad", "error"), usage_count > 1 -> should be True
    assert engine.should_i_adjust("This was wrong and bad with an error.", usage_count=5, is_recurring=False) is True
    
    # 3. Recurring -> should always be True
    assert engine.should_i_adjust("Outlier issue", usage_count=1, is_recurring=True) is True


@pytest.mark.asyncio
async def test_autonomous_weight_adjustment():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        node_path = nodes_dir / "skill-test_critique.html"
        
        # Write initial html-nn node with caution = 0.8
        node_path.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-test_critique">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["sync"],
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

        # Run process_feedback with high severity caution critique
        engine = GoalEngine(root_dir=tmpdir)
        res = await engine.process_feedback(
            feedback="This was way too cautious. It failed because of safety restrictions.",
            node_path=str(node_path),
            usage_count=5,
            is_recurring=False
        )
        
        assert "Successfully adjusted behavioral weights" in res
        
        # Read the file and verify the caution score was decremented
        content = node_path.read_text(encoding="utf-8")
        metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
        assert metadata_m is not None
        
        data = json.loads(metadata_m.group(1).strip())
        assert data["emotional_valence"]["caution"] < 0.8
        assert data["emotional_valence"]["caution"] == 0.6  # 0.8 - 0.2
        assert data["emotional_valence"]["curiosity"] == 0.2  # 0.1 + 0.1
        
        # Verify that weights were re-compiled and lora-weights script block is injected
        assert 'id="lora-weights"' in content


@pytest.mark.asyncio
async def test_non_critical_event_logging():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        node_path = nodes_dir / "skill-test_non_critical.html"
        
        node_path.write_text("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="skill-test_non_critical">
  <script type="application/ld+json" id="node-metadata">
  {
    "semantic_tags": ["sync"],
    "emotional_valence": {
      "joy": 0.1,
      "caution": 0.8,
      "curiosity": 0.1,
      "neutral": 0.1
    },
    "relevance_weight": 1.0,
    "usage_count": 1
  }
  </script>
</article>
</body>
</html>""", encoding="utf-8")

        engine = GoalEngine(root_dir=tmpdir)
        
        # Low severity critique -> logged as Non-Critical-Event
        res = await engine.process_feedback(
            feedback="a minor visual alignment issue",
            node_path=str(node_path),
            usage_count=1,
            is_recurring=False
        )
        
        assert "logged as a Non-Critical-Event" in res
        
        # Verify the file was NOT modified
        content = node_path.read_text(encoding="utf-8")
        assert 'id="lora-weights"' not in content

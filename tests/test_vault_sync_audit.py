import pytest
import tempfile
from pathlib import Path

from sdk.agent.tools import compile_lora_adapter, SecurityError

"""
The get_user_data tests that lived here were removed with the tool itself.

They asserted that a "SECURED" field returned "USA-987654321" behind a
permission token -- but that value, and every other field the tool served, was
a hardcoded dict inside the function body. The vault was never read. Three
tests named for a vault audit passed consistently while establishing nothing
about the vault.

test_pi_sanitizer_zero_leak below tests real behaviour and stays.
"""


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

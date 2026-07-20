import pytest
import tempfile
import os
from pathlib import Path

from sdk.agent.tools import compile_lora_adapter
from scripts.diagnose_html_nn import audit_file

@pytest.mark.asyncio
async def test_compile_lora_adapter_and_audit():
    # 1. Create a dummy HTML-NN file
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write("""<!DOCTYPE html>
<html>
<body>
<article data-node-id="test-compile-node" data-node-type="skill" data-execution-authority="0.20">
  <header><h1>Test Node</h1></header>
  <section id="semantic-memory">
    <p>Test description.</p>
  </section>
</article>
</body>
</html>""")
        temp_path = f.name

    try:
        # 2. Run compile_lora_adapter
        res = await compile_lora_adapter(temp_path, "test-task-123")
        assert "Successfully compiled and serialized" in res
        
        # 3. Run audit on this file
        audit_res = await audit_file(Path(temp_path))
        
        # Verify Serialization Integrity
        assert audit_res["serialization_integrity"]["weights_found"] is True
        assert audit_res["serialization_integrity"]["weights_valid"] is True
        
        # Verify Metadata Header
        assert audit_res["metadata_header"]["has_target_layer"] is True
        assert audit_res["metadata_header"]["has_rank"] is True
        assert audit_res["metadata_header"]["has_base_hash"] is True
        
        # Verify Self-Validation
        assert audit_res["self_validation"]["decode_success"] is True
        assert audit_res["self_validation"]["error"] is None
        
    finally:
        os.unlink(temp_path)

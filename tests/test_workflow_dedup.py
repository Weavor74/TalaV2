import pytest
from pathlib import Path

def test_server_workflow_dedup_check():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "executed_flow_ids" in server_py
    assert "if _flow_id in executed_flow_ids:" in server_py

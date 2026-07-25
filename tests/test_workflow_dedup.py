import pytest
from pathlib import Path

def test_server_workflow_dedup_check():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "executed_workflow_paths" in server_py
    assert "canonical_path = str(_json_path.resolve())" in server_py
    assert "if canonical_path in executed_workflow_paths:" in server_py

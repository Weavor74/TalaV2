import pytest
from pathlib import Path

def test_os_info_and_platform_tool_rules_in_system_prompt():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "=== HOST OPERATING SYSTEM & ENVIRONMENT ===" in server_py
    assert "Host Operating System:" in server_py
    assert "search_workspace_files" in server_py

import pytest
from pathlib import Path

def test_system_prompt_os_metadata_injection():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "[Host System Environment:" in server_py
    assert "platform.system()" in server_py
    assert "PowerShell" in server_py

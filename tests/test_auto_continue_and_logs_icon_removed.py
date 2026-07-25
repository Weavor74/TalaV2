import pytest
from pathlib import Path

def test_auto_continue_directive_and_system_logs_icon_removed():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert "[SYSTEM DIRECTIVE]: You stated your intention to search, locate, or edit, but you did NOT output a tool call tag" in server_py
    
    html_content = Path("renderer/index.html").read_text(encoding="utf-8")
    assert 'data-view="logs"' not in html_content

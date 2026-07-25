import pytest
from pathlib import Path

def test_archived_chat_selector_implementation():
    html_content = Path("renderer/index.html").read_text(encoding="utf-8")
    assert 'id="chat-session-select"' in html_content
    
    js_content = Path("renderer/renderer.js").read_text(encoding="utf-8")
    assert 'registerSession' in js_content
    assert 'renderSessionSelect' in js_content
    assert 'switchSession' in js_content
    assert 'chat.sessions_list' in js_content

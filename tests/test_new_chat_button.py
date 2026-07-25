import pytest
from pathlib import Path

def test_new_chat_button_exists_in_html_and_js():
    html_content = Path("renderer/index.html").read_text(encoding="utf-8")
    assert 'id="btn-chat-new"' in html_content
    
    js_content = Path("renderer/renderer.js").read_text(encoding="utf-8")
    assert 'startNewChat()' in js_content
    assert 'btn-chat-new' in js_content

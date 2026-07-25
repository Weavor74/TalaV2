import pytest
from pathlib import Path

def test_mission_control_active_steps_and_completion_rendering():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert '"result": json.loads(r["result"])' in server_py or '"result":' in server_py
    
    js_content = Path("renderer/renderer.js").read_text(encoding="utf-8")
    assert "Active Step:" in js_content
    assert "Final Result:" in js_content
    assert "<details" in js_content

import pytest
import json
from pathlib import Path

def test_electron_rpc_timeout_values():
    main_js = Path("electron/main.js").read_text(encoding="utf-8")
    assert "1_800_000" in main_js or "1800000" in main_js
    assert "customTimeout" in main_js
    assert "longRunningMethods" in main_js

def test_server_read_timeout_values():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert 'timeout_seconds' in server_py
    assert '1800.0' in server_py

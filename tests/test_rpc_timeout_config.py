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
    # Local-provider read timeout was intentionally cut from 1800.0 to 30.0
    # (commit d4be8a9) to keep instant-response probes snappy.
    assert '30.0 if p_type == "local" else 120.0' in server_py

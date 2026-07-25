import pytest
from pathlib import Path

def test_updater_script_download_logic():
    main_js = Path("electron/main.js").read_text(encoding="utf-8")
    assert "-UseBasicParsing" in main_js
    assert "SecurityProtocolType" in main_js
    assert "System.Net.WebClient" in main_js

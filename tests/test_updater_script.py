from pathlib import Path

def test_updater_script_download_logic():
    # The generated updater scripts live in electron/update_scripts.js
    # (extracted from main.js under the size ratchet, 2026-09-23).
    scripts_js = Path("electron/update_scripts.js").read_text(encoding="utf-8")
    assert "-UseBasicParsing" in scripts_js
    assert "SecurityProtocolType" in scripts_js
    assert "System.Net.WebClient" in scripts_js

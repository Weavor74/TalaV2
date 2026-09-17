"""Highs H7: lease + network_guard remain fail-open by default; document that."""
from __future__ import annotations

from python.harness import network_guard as ng
from sdk.agent import lease_policy as LP


def test_lease_default_mode_is_open(monkeypatch):
    monkeypatch.delenv("TALA_LEASE_MODE", raising=False)
    assert LP.current_mode() == LP.MODE_OPEN


def test_network_guard_default_rules_are_off():
    assert ng.DEFAULT_RULES["enabled"] is False
    assert str(ng.DEFAULT_RULES["default"]).lower() == "allow"


def test_docs_and_settings_warn_about_unsafe_defaults():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    lease = (root / "sdk/agent/lease_policy.py").read_text(encoding="utf-8")
    guard = (root / "python/harness/network_guard.py").read_text(encoding="utf-8")
    html = (root / "renderer/index.html").read_text(encoding="utf-8")
    assert "Unsafe default" in lease or "unsafe default" in lease.lower()
    assert "Highs H7" in guard or "unsafe default" in guard.lower()
    assert "guardrails-unsafe-defaults-warning" in html

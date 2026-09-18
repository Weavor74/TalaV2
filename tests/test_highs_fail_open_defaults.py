"""Highs H7: lease + network_guard prefer fail-closed defaults; relax via Settings/env."""
from __future__ import annotations

from pathlib import Path

from python.harness import network_guard as ng
from sdk.agent import lease_policy as LP


def test_lease_default_mode_is_enforce(monkeypatch):
    monkeypatch.delenv("TALA_LEASE_MODE", raising=False)
    assert LP.current_mode() == LP.MODE_ENFORCE


def test_unknown_lease_mode_fails_closed_to_enforce(monkeypatch):
    monkeypatch.setenv("TALA_LEASE_MODE", "banana")
    assert LP.current_mode() == LP.MODE_ENFORCE


def test_network_guard_default_rules_are_on_and_deny():
    assert ng.DEFAULT_RULES["enabled"] is True
    assert str(ng.DEFAULT_RULES["default"]).lower() == "deny"


def test_docs_and_settings_document_safer_defaults_and_relax_path():
    root = Path(__file__).resolve().parents[1]
    lease = (root / "sdk/agent/lease_policy.py").read_text(encoding="utf-8")
    guard = (root / "python/harness/network_guard.py").read_text(encoding="utf-8")
    html = (root / "renderer/index.html").read_text(encoding="utf-8")
    env = (root / ".env.example").read_text(encoding="utf-8")
    assert "Highs H7" in lease or "fail-closed" in lease.lower()
    assert "default-deny" in guard.lower() or "default: deny" in guard
    assert "guardrails-unsafe-defaults-warning" in html
    assert "TALA_LEASE_MODE=open" in env

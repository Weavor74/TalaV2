from python.harness.server import _tier_sort_providers


def test_sorts_remote_before_local_before_embedded():
    providers = [
        {"id": "local-a", "type": "local"},
        {"id": "embedded-builtin", "type": "embedded"},
        {"id": "remote-a", "type": "remote"},
    ]
    result = _tier_sort_providers(providers)
    assert [p["id"] for p in result] == ["remote-a", "local-a", "embedded-builtin"]


def test_stable_within_tier():
    """Providers of the same type keep their relative input order."""
    providers = [
        {"id": "local-b", "type": "local"},
        {"id": "remote-b", "type": "remote"},
        {"id": "local-a", "type": "local"},
        {"id": "remote-a", "type": "remote"},
    ]
    result = _tier_sort_providers(providers)
    assert [p["id"] for p in result] == ["remote-b", "remote-a", "local-b", "local-a"]


def test_robust_against_drifted_db_priority():
    """Regression test for a real production bug: inference.save_providers
    persists priority as a pure positional index with no type-awareness, so
    a Settings UI save (e.g. toggling a FORCE checkbox) can silently push a
    remote provider's priority number past every local model's. Confirmed
    against live DB state: Gemini ended up at priority 11 (dead last, after
    every local model and even the embedded fallback). The tier sort must
    still put it ahead of every local provider regardless of that drift."""
    providers = [
        {"id": "kimi", "type": "remote", "priority": 1},
        {"id": "ollama-a", "type": "local", "priority": 2},
        {"id": "ollama-b", "type": "local", "priority": 3},
        {"id": "embedded-builtin", "type": "embedded", "priority": 10},
        {"id": "gemini", "type": "remote", "priority": 11},  # drifted to last
    ]
    result = _tier_sort_providers(providers)
    assert [p["id"] for p in result] == ["kimi", "gemini", "ollama-a", "ollama-b", "embedded-builtin"]

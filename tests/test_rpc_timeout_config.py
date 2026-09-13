from pathlib import Path

def test_electron_rpc_timeout_values():
    # The method -> budget table moved to rpc_policy.js (size-ratchet
    # extraction); main.js must consume the policy rather than keep a copy.
    policy_js = Path("electron/rpc_policy.js").read_text(encoding="utf-8")
    assert "1_800_000" in policy_js or "1800000" in policy_js
    assert "LONG_RUNNING_METHODS" in policy_js
    assert "desktop.run" in policy_js and "inference.generate" in policy_js
    main_js = Path("electron/main.js").read_text(encoding="utf-8")
    assert "rpcTimeoutMs(" in main_js
    assert "longRunningMethods" not in main_js  # no drifting second copy

def test_server_read_timeout_values():
    server_py = Path("python/harness/server.py").read_text(encoding="utf-8")
    assert 'timeout_seconds' in server_py
    # Commit d4be8a9 conflated two different timeouts: the reachability PROBE
    # (a lightweight /api/show ping -- correctly kept fast) and the actual
    # generation READ timeout (cut to 30.0, incorrectly -- CPU-bound local
    # inference routinely needs 15-25s+ just to prefill a ~2000-token prompt
    # before generation even starts, confirmed against live Ollama server
    # logs showing a 100% failure rate at exactly the old 30.0s cutoff).
    # The probe stays fast; only the read timeout was widened back out.
    assert 'timeout=3.0) as _probe' in server_py  # probe stays fast
    assert '180.0 if p_type == "local" else 120.0' in server_py  # read timeout has real headroom

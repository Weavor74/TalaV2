"""
The risk classifier on ``shell.run`` must have teeth (P2 review item 22).

RiskClassifier.classify_action computed a tier and handle_shell_run logged it —
and nothing anywhere read the result, so a critical-tier command ran exactly
like a low-tier one. PolicyEvaluator.evaluate_command existed with zero
callers. These tests pin the enforcement now wired into handle_shell_run:

  1. known-destructive patterns are refused outright, approval or not;
  2. critical-tier commands need an explicit approved=true;
  3. approval lets a critical command through to the executor;
  4. sub-critical commands run without any approval flag.
"""

import json

import pytest

from python.harness import server


def _parse(raw: str) -> dict:
    return json.loads(raw)["result"]


async def _fake_run(command, cwd, timeout=60.0):
    return {
        "success": True,
        "exit_code": 0,
        "stdout": f"ran: {command}",
        "stderr": "",
        "timed_out": False,
    }


@pytest.fixture
def executor_spy(monkeypatch):
    """Replace the real executor so no test ever spawns a process."""
    calls = []

    async def spy(command, cwd, timeout=60.0):
        calls.append(command)
        return await _fake_run(command, cwd, timeout)

    from sdk.agent.async_shell import AsyncShellExecutor
    monkeypatch.setattr(AsyncShellExecutor, "run", spy)
    return calls


@pytest.mark.asyncio
async def test_destructive_pattern_is_blocked_even_with_approval(executor_spy):
    raw = await server.handle_shell_run(
        {"command": "rm -rf / --no-preserve-root", "approved": True}, 1
    )
    result = _parse(raw)
    assert result["blocked"] is True
    assert result["success"] is False
    assert executor_spy == [], "a policy-blocked command reached the executor"


@pytest.mark.asyncio
async def test_critical_tier_requires_explicit_approval(executor_spy):
    # "rm" makes the classifier grade this critical; the path is harmless,
    # which is exactly the point — the tier, not the outcome, is what gates.
    raw = await server.handle_shell_run({"command": "rm stale.tmp"}, 2)
    result = _parse(raw)
    assert result["blocked"] is True
    assert result["risk_level"] == "critical"
    assert "approved=true" in result["stderr"]
    assert executor_spy == []


@pytest.mark.asyncio
async def test_approved_critical_command_runs(executor_spy):
    raw = await server.handle_shell_run(
        {"command": "rm stale.tmp", "approved": True}, 3
    )
    result = _parse(raw)
    assert result["success"] is True
    assert result["risk_level"] == "critical"
    assert executor_spy == ["rm stale.tmp"]


@pytest.mark.asyncio
async def test_sub_critical_command_needs_no_approval(executor_spy):
    raw = await server.handle_shell_run({"command": "echo hello"}, 4)
    result = _parse(raw)
    assert result["success"] is True
    assert result["risk_level"] == "medium"
    assert executor_spy == ["echo hello"]

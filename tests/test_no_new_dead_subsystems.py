"""
tests/test_no_new_dead_subsystems.py — the ratchet.

Nine subsystems have now been found built, correct, tested and never called.
Every one was caught by hand, months late, by someone tracing a value to its
consumer and finding none. Unit tests never caught any of them, because each
component worked; what was missing was a caller, and no component-level test
can see that.

This is the guard that would have caught all nine on the commit that
introduced them. It does not demand that the tree be clean -- 170 entry points
are unreached today, and some of those are legitimate (aiortc event callbacks,
CLI entry points, defensive helpers). What it demands is that the number does
not grow silently.

WHEN THIS FAILS

You added a function nothing calls. Either:
  * wire it to a consumer, which is usually what you meant to do; or
  * delete it, if it turned out not to be needed; or
  * regenerate the baseline with `python scripts/reachability_audit.py
    --baseline` AND say in the commit message why the new entry is reachable
    by a route the audit cannot see.

The third option is the one to be suspicious of. It is how all nine got in.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "reachability_baseline.json"
AUDIT = ROOT / "scripts" / "reachability_audit.py"


_CACHE: list[dict] | None = None


def _current() -> list[dict]:
    """
    Run the audit once per session.

    It walks every .py under sdk/ and python/ and regex-searches each name
    across the whole corpus, which costs about thirty seconds. Running that
    per-test doubled the cost of the suite's slowest file for no extra signal.
    """
    global _CACHE
    if _CACHE is None:
        result = subprocess.run(
            [sys.executable, str(AUDIT), "--json"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=300,
        )
        assert result.returncode == 0, f"the audit itself failed:\n{result.stderr}"
        _CACHE = json.loads(result.stdout)
    return _CACHE


def _key(finding: dict) -> str:
    return f"{finding['module']}::{finding['name']}"


@pytest.mark.slow
def test_no_newly_unreachable_code():
    baseline = set(json.loads(BASELINE.read_text(encoding="utf-8")))
    current = {_key(f): f for f in _current()}

    added = sorted(set(current) - baseline)
    assert not added, (
        "these public entry points have no caller anywhere -- not another "
        "module, not the renderer, not a script, not a workflow:\n\n"
        + "\n".join(
            f"  {current[k]['module']}:{current[k]['line']}  {current[k]['name']}"
            + ("  (only a test calls it)" if current[k]["test_only"] else "")
            for k in added
        )
        + "\n\nWire it to a consumer, or delete it. If it really is reachable "
          "by a route this audit cannot see, regenerate the baseline with "
          "`python scripts/reachability_audit.py --baseline` and say why in "
          "the commit message."
    )


@pytest.mark.slow
def test_the_baseline_does_not_rot():
    """
    Entries that have been wired or deleted must leave the baseline.

    Without this the file only ever grows, and a stale allowlist is how a
    ratchet quietly stops ratcheting.
    """
    baseline = set(json.loads(BASELINE.read_text(encoding="utf-8")))
    current = {_key(f) for f in _current()}

    stale = sorted(baseline - current)
    assert not stale, (
        f"{len(stale)} baseline entries are now reachable or gone. Good news: "
        "regenerate with `python scripts/reachability_audit.py --baseline` so "
        "the ratchet keeps its grip.\n\n"
        + "\n".join(f"  {s}" for s in stale[:40])
        + ("\n  ..." if len(stale) > 40 else "")
    )


def test_documentation_mentions_are_not_evidence_of_a_caller():
    """
    The trap that made the first version of this audit useless.

    Tala's self-documenting wiki names every function in the tree, so counting
    a mention in nodes/*.html as a caller marks the entire codebase reachable
    and the audit reports a clean tree with 170 dead entry points in it.
    """
    src = AUDIT.read_text(encoding="utf-8")
    assert "nodes/*.html is excluded on purpose" in src or "NOT evidence" in src
    assert '"nodes/' not in src.split("CODE_ROOTS")[-1].split("def ")[0], (
        "the audit is scanning nodes/ as a call site again"
    )

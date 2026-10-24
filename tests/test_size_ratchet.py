"""
File-size ratchet for the Python side (P2 review item 16).

An 18,000-line server.py did not happen in one commit; it happened because no
commit was the one that crossed a line. This test IS the line. Every Python
file gets DEFAULT_BUDGET lines; the files that were already over it when the
ratchet landed are pinned at the size they had that day.

The rules of the ratchet:
  * A pinned number may go DOWN (please lower it when you shrink the file).
  * It may never go UP. If this test fails your change, move code out into a
    module — python/harness/ already has per-domain homes for most of
    server.py — instead of raising the pin.
  * New files live inside DEFAULT_BUDGET. No new entries in PINNED.

The JS equivalent lives in eslint.config.mjs (max-lines).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DEFAULT_BUDGET = 1500

# file → the line count it had when the ratchet landed (2026-09-12). Down only.
PINNED = {
    "python/harness/db.py": 1598,
    "python/harness/server.py": 15851,
    "sdk/agent/documentation_nodes.py": 1956,
    "sdk/agent/goal_engine.py": 1530,
    "sdk/agent/graph_engine.py": 1785,
    "sdk/agent/tools.py": 4870,
}

SCANNED_TREES = ("python", "sdk", "scripts")


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def _scanned_files():
    for base in SCANNED_TREES:
        for path in sorted((REPO / base).rglob("*.py")):
            posix = path.relative_to(REPO).as_posix()
            if "__pycache__" in posix or "egg-info" in posix:
                continue
            yield posix, path


def test_no_python_file_grows_past_its_budget():
    offenders = []
    for posix, path in _scanned_files():
        budget = PINNED.get(posix, DEFAULT_BUDGET)
        lines = _line_count(path)
        if lines > budget:
            offenders.append(f"{posix}: {lines} lines (budget {budget})")
    assert not offenders, (
        "File(s) grew past the size ratchet. Move code out into a module "
        "instead of raising the budget:\n  " + "\n  ".join(offenders)
    )


def test_pins_are_not_stale():
    """A pin more generous than the file needs is quiet room to regrow into.
    Once a file shrinks, the pin must come down with it (or off entirely,
    once it fits DEFAULT_BUDGET)."""
    stale = []
    for posix, pinned in PINNED.items():
        path = REPO / posix
        if not path.exists():
            stale.append(f"{posix}: pinned but no longer exists — remove the entry")
            continue
        lines = _line_count(path)
        # 200 lines of slack so ordinary shrinking doesn't demand a pin edit
        # every commit; a big extraction should bring the pin down with it.
        if lines <= pinned - 200:
            stale.append(
                f"{posix}: is {lines} lines but pinned at {pinned} — lower the pin"
            )
        if lines <= DEFAULT_BUDGET:
            stale.append(
                f"{posix}: now fits DEFAULT_BUDGET ({lines} lines) — remove the pin"
            )
    assert not stale, "Stale ratchet pins:\n  " + "\n  ".join(stale)

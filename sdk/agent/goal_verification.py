"""
goal_verification.py — did the run do what the goal asked, or merely not fail?

WHY THIS EXISTS
---------------
``GoalEngine.run_goal`` sets ``success = True`` before the first phase and only
ever clears it when a phase fails or the budget runs out. Success is therefore
the *absence of failure*, never the presence of achievement, and a goal whose
decomposition covered half of what was asked reports exactly the same as one
that did everything.

The benchmark that exposed it:

    "Create a new file named common.py with a Logger class. Then refactor both
     user.py and order.py to import the Logger from common.py."

One generation, 27 seconds. ``common.py`` was written, ``user.py`` and
``order.py`` were never opened, and the result said ``success: True``. Both
engines, every run, byte-identical -- the model is barely involved in the
decision, so this is not a capability failure.

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
Files named in the goal text, hashed before and after the run. That is a narrow
signal, chosen because it is *deterministic and cheap*: no second model call, no
judgement about intent, and it works identically on a machine with no GPU. It
cannot tell whether the Logger class is any good. It can tell that a file the
goal said to create does not exist, and that a file the goal said to change is
byte-identical to how it started.

The read/write distinction is where a naive version does damage. "Read user.py
and explain it" names a file that *should* be untouched, and flagging that would
turn correct runs into failures. So an untouched file is only held against the
run when the goal also carries a mutation verb; otherwise it is reported as
context and nothing more.

A missing artifact the goal asked to create is unambiguous and always fails.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Extensions worth treating as an artifact the goal is talking about. Bounded
#: on purpose: a goal mentioning "the 3.11 runtime" should not have "3.11" read
#: as a file.
_ARTIFACT_RE = re.compile(
    r"\b([\w\-./\\]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|html|css|sql|sh|ps1))\b",
    re.IGNORECASE,
)

#: Verbs that mean the goal intends to *change* something that already exists.
#: An untouched file is only a failure in their presence.
_MUTATION_VERBS = (
    "refactor", "modify", "update", "change", "edit", "rewrite", "fix",
    "patch", "convert", "migrate", "rename", "replace", "remove", "delete",
    "add to", "import", "wire", "adjust", "correct", "repair", "extend",
)

#: Verbs that mean the goal intends to bring something into existence.
_CREATION_VERBS = ("create", "write", "generate", "make", "add", "produce", "build", "scaffold")


@dataclass
class GoalVerification:
    """What the goal named, and what actually happened to it."""

    named: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: False only when the evidence is unambiguous. See `reason`.
    ok: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "named": self.named,
            "created": self.created,
            "modified": self.modified,
            "untouched": self.untouched,
            "missing": self.missing,
            "ok": self.ok,
            "reason": self.reason,
        }


def named_artifacts(goal_text: str) -> list[str]:
    """Files the goal talks about, in order, without repeats."""
    found: list[str] = []
    for match in _ARTIFACT_RE.findall(str(goal_text or "")):
        normalised = match.replace("\\", "/").strip("./")
        if normalised and normalised not in found:
            found.append(normalised)
    return found


def _digest(path: Path) -> str | None:
    """Content hash, or None when the file is not there."""
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        # Unreadable is not the same as absent, but for this purpose both mean
        # "cannot show it changed", and guessing would be worse than saying so.
        return None


def snapshot(root_dir: str | Path, names: list[str]) -> dict[str, str | None]:
    """Hash each named artifact relative to the workspace root."""
    root = Path(root_dir)
    return {name: _digest(root / name) for name in names}


def _mentions(goal_low: str, verbs: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(v)}\b", goal_low) for v in verbs)


def verify(
    goal_text: str,
    before: dict[str, str | None],
    after: dict[str, str | None],
) -> GoalVerification:
    """
    Compare the two snapshots and decide whether the goal was demonstrably met.

    Only two things fail a run, and both are unambiguous:

    * the goal used a creation verb and a named artifact still does not exist;
    * the goal used a mutation verb and a named artifact that existed before is
      byte-identical after.

    Everything else is reported and nothing more. A verifier that guesses turns
    working runs red, which would be a worse failure than the one it fixes.
    """
    goal_low = str(goal_text or "").lower()
    report = GoalVerification(named=list(before.keys()))

    for name in before:
        was, now = before.get(name), after.get(name)
        if was is None and now is not None:
            report.created.append(name)
        elif was is not None and now is None:
            report.missing.append(name)
        elif was is None and now is None:
            report.missing.append(name)
        elif was != now:
            report.modified.append(name)
        else:
            report.untouched.append(name)

    if not report.named:
        return report

    wants_creation = _mentions(goal_low, _CREATION_VERBS)
    wants_mutation = _mentions(goal_low, _MUTATION_VERBS)

    if wants_creation and report.missing:
        report.ok = False
        report.reason = (
            f"the goal asked for {', '.join(report.missing)} but "
            f"{'it does' if len(report.missing) == 1 else 'they do'} not exist "
            f"after the run"
        )
        return report

    # Only artifacts that were present at the start can be "left unchanged";
    # a file that never existed is covered by the missing check above.
    stale = [n for n in report.untouched if before.get(n) is not None]
    if wants_mutation and stale:
        report.ok = False
        report.reason = (
            f"the goal asked to change {', '.join(stale)} but "
            f"{'it is' if len(stale) == 1 else 'they are'} byte-identical to "
            f"before the run"
        )
        return report

    return report

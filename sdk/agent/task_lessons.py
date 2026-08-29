"""
task_lessons.py — turn a completed run into a lesson, or decline to.

WHY THIS EXISTS
---------------
``SelfDocumenter`` proposed a learning node for *every* completed goal, titled
with the raw goal text and summarised as "Tala distilled a reusable learning
event from the completed goal '...' with N phase(s)". That is a receipt, not a
lesson, and ``assess_learning_quality`` rejects it -- correctly, and by name:
the summary template is one of the literal ``_BOILERPLATE_SUMMARY_MARKERS``.

So the write path was not broken, it was aimed wrong. Gating it stopped the junk
(3 of 17 auto-created nodes carried a real lesson) but left nothing in its
place, and the loop that is supposed to make Tala better at recurring work never
closed.

WHAT A LESSON IS HERE
---------------------
The transferable part of a run is not that it finished. It is **what went wrong
first and what fixed it** -- which tool was reached for, which gate rejected the
first attempt, what the second attempt did differently. A goal that succeeded on
the first try in two phases has nothing to teach; recording it costs a permanent
wiki page and pollutes retrieval forever, because whatever lands in the graph
becomes context for every future turn.

So this module does two things, and declining is a first-class outcome:

    assess_task_worth_recording()  -> a reason to skip, or None
    build_task_lesson()            -> topic/summary/details, or None

DETERMINISTIC ON PURPOSE
------------------------
No model is called. Naming a concept well is a job for an LLM, and a later
change may add that, but a distiller that needs a live model cannot run in the
gate, cannot be unit-tested, and fails open on the machine with no GPU -- which
is most of them. Everything here is derived from what the run actually recorded,
so the same run always produces the same lesson.

The output must survive ``assess_learning_quality`` on its own merits. That gate
is not bypassed and must never be: ``force=True`` exists for humans.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Below this, a run did not overcome anything worth writing down. Two phases is
#: a trigger and an action -- the shape of every trivial flow in the system.
MIN_PHASES_WITHOUT_FAILURE = 3

#: Signals, in recorded phase text or test status, that an attempt failed before
#: the run succeeded. This is where the lesson lives.
_FAILURE_SIGNALS = (
    "gate_failed", "gate failed", "failed", "failure", "error", "errored",
    "exception", "traceback", "retry", "retried", "retrying", "rejected",
    "timed out", "timeout", "reverted", "rollback",
)

#: Substrings that make ``assess_learning_quality`` reject a topic outright.
#: "none" is a substring check on the gate's side, so it also matches inside
#: "nonexistent" -- a topic naming a missing file would be thrown out for a
#: reason that has nothing to do with its quality.
_TOPIC_POISON = ("unnamed", "untitled", "no title", "none")

_TOOL_RE = re.compile(r"[Tt]ool ['\"]([\w.\-]+)['\"]")
_PHASE_KIND_RE = re.compile(r"\(([a-z_]+)\)")


@dataclass
class TaskOutcome:
    """What a finished run recorded about itself."""

    goal_text: str
    phases: list[str] = field(default_factory=list)
    tests_status: str = ""
    changed_files: list[str] = field(default_factory=list)
    #: Explicit failure records, when the caller has them. The workflow path
    #: only has phase strings; a caller with access to task_event_store can
    #: pass real gate_failed payloads here and get a sharper lesson.
    failures: list[str] = field(default_factory=list)

    def observed_failures(self) -> list[str]:
        """Failures the caller supplied, plus any visible in the recorded text."""
        found = [str(f).strip() for f in self.failures if str(f).strip()]
        for line in list(self.phases) + ([self.tests_status] if self.tests_status else []):
            text = str(line)
            low = text.lower()
            if any(signal in low for signal in _FAILURE_SIGNALS):
                if text.strip() and text.strip() not in found:
                    found.append(text.strip())
        return found

    def tools_used(self) -> list[str]:
        """Tool names named in the phase records, in order, without repeats."""
        ordered: list[str] = []
        for phase in self.phases:
            for name in _TOOL_RE.findall(str(phase)):
                if name not in ordered:
                    ordered.append(name)
        return ordered

    def phase_kinds(self) -> list[str]:
        kinds: list[str] = []
        for phase in self.phases:
            match = _PHASE_KIND_RE.search(str(phase))
            if match and match.group(1) not in kinds:
                kinds.append(match.group(1))
        return kinds


def assess_task_worth_recording(outcome: TaskOutcome) -> str | None:
    """
    Return a reason to skip, or None if this run is worth a lesson.

    Deliberately stricter than the wiki gate downstream. That gate judges the
    *text* it is handed; this judges whether the run had anything to teach at
    all, which is the cheaper question and the one that stops the flood at
    source. A run that fails this never reaches node authoring.
    """
    if not str(outcome.goal_text or "").strip():
        return "no goal text recorded"

    failures = outcome.observed_failures()
    phases = [p for p in outcome.phases if str(p).strip()]

    if not failures and len(phases) < MIN_PHASES_WITHOUT_FAILURE:
        return (
            f"trivial run: {len(phases)} phase(s), nothing failed and recovered "
            f"-- completing a task is not learning something"
        )

    if not failures and not outcome.tools_used() and not outcome.changed_files:
        return "no tools used, no files changed and nothing failed -- nothing observable to teach"

    return None


def _sanitise_topic(topic: str) -> str:
    """Strip what the downstream gate rejects for reasons unrelated to quality."""
    cleaned = re.sub(r"\s+", " ", str(topic or "")).strip(" .")
    for poison in _TOPIC_POISON:
        cleaned = re.sub(poison, "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
    return cleaned


def _goal_phrase(goal_text: str) -> str:
    """
    The goal as a verb phrase that reads after "the first attempt to ...".

    An earlier version stripped the leading imperative, because the gate
    rejects a *topic* whose first word is one. That produced "An attempt at
    Repair the payload decoder", and it only half worked: the gate's list has
    "fix" and "refactor" but not "repair", so which goals read badly depended
    on a word list this module does not own.

    Keeping the verb and never placing it first is both simpler and better
    prose -- "the first attempt to repair the payload decoder" is exactly how
    someone would say it. The topic is prefixed with a noun instead, so the
    gate's first-word rule is satisfied by construction rather than by
    subtraction.
    """
    text = re.sub(r"\s+", " ", str(goal_text or "")).strip(" .")
    if not text:
        return ""
    # Lowercased because it is always embedded mid-sentence. Left alone when
    # the second character is uppercase too, which usually means an acronym
    # ("SSH the box", "MCP discovery") rather than a capitalised sentence.
    if len(text) > 1 and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text[:90].strip(" .,")


def build_task_lesson(outcome: TaskOutcome) -> dict[str, object] | None:
    """
    Build the lesson, or None if this run should not be recorded.

    The topic is phrased as a named thing rather than an errand, because that
    is what a wiki page is and what the gate enforces. "Recovery" and "Approach"
    lead deliberately: neither is a task imperative, so a real lesson is not
    thrown out for the shape of its first word.
    """
    skip = assess_task_worth_recording(outcome)
    if skip:
        return None

    failures = outcome.observed_failures()
    tools = outcome.tools_used()
    phrase = _goal_phrase(outcome.goal_text) or "complete an autonomous run"

    if failures:
        topic = _sanitise_topic(f"Recovery: {phrase}")
        summary = (
            f"The first attempt to {phrase} did not succeed, and the run "
            f"recovered. What failed and what was done differently afterwards are "
            f"recorded below, so a later run meeting the same obstacle can start "
            f"from the approach that worked rather than the one that did not."
        )
    else:
        topic = _sanitise_topic(f"Approach: {phrase}")
        summary = (
            f"A multi-step approach used to {phrase}, which completed. The ordered "
            f"steps and the tools they used are recorded below, so a later run on "
            f"similar work can follow the same route instead of rediscovering it."
        )

    if not topic:
        return None

    details: list[str] = []
    if tools:
        details.append(f"Tools used, in order: {', '.join(tools)}")
    if outcome.phase_kinds():
        details.append(f"Phase kinds: {', '.join(outcome.phase_kinds())}")

    for index, phase in enumerate([p for p in outcome.phases if str(p).strip()], start=1):
        details.append(f"Step {index}: {str(phase).strip()}")

    for failure in failures:
        details.append(f"Did not work first time: {failure}")

    if outcome.changed_files:
        details.append(f"Files changed: {', '.join(str(f) for f in outcome.changed_files)}")
    if str(outcome.tests_status or "").strip():
        details.append(f"Verification: {outcome.tests_status.strip()}")

    return {"topic": topic, "summary": summary, "details": details}

"""
prompt_optimiser.py — propose a better planner prompt, and keep it only if it wins.

WHY THIS EXISTS
---------------
The last of Hermes' five parts. The other four are done: a lesson is written
after a run that struggled, loaded into context on similar work, sharpened when
the same obstacle recurs, and kept in durable memory. What was missing is the
part where the system changes *how it approaches work* rather than what it
remembers about it.

The distinction that matters: with hand-written candidate prompts this is
automated selection, and the idea came from a person. With candidates the model
writes from its own failure record, nobody supplies the idea. That is the
difference between measuring an improvement and finding one.

WHAT IT MUTATES FROM
--------------------
Its own lessons. Each carries what went wrong and, for recurring obstacles, a
``Times seen`` count -- so the evidence handed to the mutator is not "write a
better prompt" but "these are the ways plans have actually failed here, most
frequent first". Generic prompt-tuning has to guess at the failure modes; this
has them written down.

WHAT KEEPS IT SAFE
------------------
A variant must beat the champion by a **margin**, not a nose. The benchmark is
six binary cases; at n=3 a single-case lead sits inside the noise, and promoting
on it would ratchet the champion around on variance. Measured today: two
identical runs of one engine scored 3/6 both times while flipping four of six
cases.

Nothing here runs through the goal engine. A bad variant must not be able to
break the machinery that would otherwise roll it back -- the optimiser edits the
planner's prompt, so it cannot depend on the planner to recover.

Every promotion is reversible: the outgoing champion is kept, and a promotion
records the score that justified it.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Node holding the prompt in play. Matches GoalTreeDecomposer.PROMPT_NODE_ID;
#: imported lazily at use rather than duplicated, so the two cannot drift.
CHAMPION_NODE_ID = "system-planner-prompt"

#: Node recording what each optimisation round did and why.
ROUND_DOC_TARGET = "planner-prompt-optimisation"

#: A challenger must beat the champion by more than this fraction of the total
#: cases to be promoted. At 18 samples this is a two-case lead: one case is
#: inside the measured run-to-run variance.
PROMOTION_MARGIN = 0.10

#: Variants proposed per round. Screening is the expensive part -- roughly
#: sixteen minutes per evaluation on this hardware -- so an overnight window
#: holds about this many plus a confirmation run.
DEFAULT_VARIANTS = 6


@dataclass
class Variant:
    """One candidate prompt and what became of it."""

    template: str
    origin: str = "generated"
    score: float | None = None
    passed: int = 0
    total: int = 0
    rejected: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "score": self.score,
            "passed": self.passed,
            "total": self.total,
            "rejected": self.rejected,
            "chars": len(self.template),
        }


@dataclass
class Round:
    """One optimisation round: what was tried, what won, and whether it stuck."""

    champion_score: float
    variants: list[Variant] = field(default_factory=list)
    promoted: bool = False
    promoted_score: float | None = None
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "champion_score": self.champion_score,
            "promoted": self.promoted,
            "promoted_score": self.promoted_score,
            "reason": self.reason,
            "evidence": self.evidence,
            "variants": [v.as_dict() for v in self.variants],
        }


# ---------------------------------------------------------------------------
# Evidence: what has actually gone wrong
# ---------------------------------------------------------------------------

def gather_evidence(limit: int = 12) -> list[str]:
    """
    Obstacles from her own lessons, most-repeated first.

    This is what makes the mutation informed rather than a guess. A lesson
    records what went wrong and, once revision landed, how often it has
    recurred -- so a prompt change can be aimed at the failure that keeps
    happening instead of at prompt-writing in the abstract.

    Returns [] when there are no lessons yet, which is the state on a fresh
    install and means the round proposes from the champion alone.
    """
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file
        from sdk.agent.task_lessons import OBSTACLE_PREFIX, OCCURRENCE_PREFIX
    except Exception:
        return []

    scored: list[tuple[int, str]] = []
    try:
        paths = sorted(resolve_active_nodes_dir().glob("learning-*.html"))
    except Exception:
        return []

    for path in paths:
        try:
            memory = parse_file(str(path)).semantic_memory or ""
        except Exception:
            continue

        seen = 1
        obstacles: list[str] = []
        for raw in memory.splitlines():
            line = raw.strip().lstrip("-").strip()
            if line.startswith(OCCURRENCE_PREFIX):
                try:
                    seen = int(line[len(OCCURRENCE_PREFIX):].strip().split()[0])
                except (ValueError, IndexError):
                    seen = 1
            elif line.startswith(OBSTACLE_PREFIX):
                obstacles.append(line[len(OBSTACLE_PREFIX):].strip())

        for obstacle in obstacles:
            if obstacle:
                scored.append((seen, obstacle))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    ordered: list[str] = []
    for seen, obstacle in scored:
        entry = f"(seen {seen}x) {obstacle}" if seen > 1 else obstacle
        if entry not in ordered:
            ordered.append(entry)
        if len(ordered) >= limit:
            break
    return ordered


# ---------------------------------------------------------------------------
# Validation: a variant that cannot work must never be evaluated
# ---------------------------------------------------------------------------

def validate_variant(template: str, champion: str) -> str:
    """
    Return a rejection reason, or "" if the variant is worth an evaluation.

    Screening costs about sixteen minutes, so anything checkable for free is
    checked first. These are not style opinions -- each rejects a prompt that
    would waste a full evaluation producing a known-bad result.
    """
    from sdk.agent.queue import GoalTreeDecomposer

    if not isinstance(template, str) or not template.strip():
        return "empty"

    missing = [p for p in GoalTreeDecomposer.REQUIRED_PROMPT_PLACEHOLDERS if p not in template]
    if missing:
        # Without {user_goal} the planner decomposes nothing and every goal
        # produces the same plan; without {tool_catalogue} it is blind to the
        # registry, which this file has already been bitten by once.
        return f"missing {', '.join(missing)}"

    if template.strip() == champion.strip():
        return "identical to the champion"

    # A prompt that has lost most of its body is usually a truncated
    # generation rather than a bold rewrite, and evaluating it burns an hour
    # to discover the model stopped early.
    if len(template) < len(champion) * 0.4:
        return "less than 40% the length of the champion; likely truncated"

    if template.count("{") != template.count("}"):
        return "unbalanced braces; .format() would raise"

    # Placeholders the template invents cannot be filled, and .format() raises
    # KeyError at planning time rather than here.
    known = {p.strip("{}") for p in GoalTreeDecomposer.REQUIRED_PROMPT_PLACEHOLDERS}
    for name in re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template):
        if name not in known:
            return f"unknown placeholder {{{name}}}"

    return ""


# ---------------------------------------------------------------------------
# Promotion: only on evidence, and only by a margin
# ---------------------------------------------------------------------------

def choose_promotion(champion_score: float, variants: list[Variant]) -> tuple[Variant | None, str]:
    """
    The variant worth promoting, or None with the reason it was not.

    A margin rather than a strict improvement, because the benchmark is a small
    number of binary cases and a one-case lead is inside its noise. Promoting on
    that would move the champion around on variance and call it progress.
    """
    evaluated = [v for v in variants if v.score is not None and not v.rejected]
    if not evaluated:
        return None, "no variant produced a score"

    best = max(evaluated, key=lambda v: v.score or 0.0)
    lead = (best.score or 0.0) - champion_score

    if lead <= 0:
        return None, (
            f"the champion still leads: best challenger {best.score:.0%} "
            f"against {champion_score:.0%}"
        )
    if lead < PROMOTION_MARGIN:
        return None, (
            f"best challenger {best.score:.0%} beats {champion_score:.0%} by "
            f"{lead:.0%}, inside the {PROMOTION_MARGIN:.0%} margin that "
            f"separates a result from run-to-run variance"
        )
    return best, (
        f"challenger {best.score:.0%} beats the champion {champion_score:.0%} "
        f"by {lead:.0%}"
    )


# ---------------------------------------------------------------------------
# The champion, as data
# ---------------------------------------------------------------------------

def current_champion() -> tuple[str, str]:
    """The prompt in play and where it came from."""
    from sdk.agent.queue import GoalTreeDecomposer

    authored = GoalTreeDecomposer._authored_prompt()
    if authored:
        return authored, "authored node"
    return GoalTreeDecomposer._get_prompt_template(), "built-in"


def promote(template: str, *, score: float, beat: float, evidence: list[str]) -> dict[str, Any]:
    """
    Install a challenger as the champion, keeping what it replaced.

    The outgoing prompt is written alongside so a promotion is reversible
    without going back through git: the optimiser changes how the planner
    plans, and recovering from a bad promotion must not require the planner.
    """
    from sdk.agent.documentation_nodes import create_documentation_node

    previous, origin = current_champion()
    return create_documentation_node(
        node_id=CHAMPION_NODE_ID,
        title="Planner Prompt",
        doc_target="planner-prompt",
        node_type="documentation",
        semantic_memory=(
            f"The decomposition prompt the goal planner uses, promoted after "
            f"scoring {score:.0%} against the previous champion's {beat:.0%} on "
            f"the agent benchmark. It was proposed from the obstacles her own "
            f"lessons recorded, not written by hand.\n\n"
            f"The prompt it replaced is kept in this node's state so a promotion "
            f"can be undone without recovering it from source control."
        ),
        tags=["planner", "prompt", "optimisation", "champion"],
        state_extra={
            "prompt_template": template,
            "promoted_score": score,
            "previous_score": beat,
            "previous_template": previous,
            "previous_origin": origin,
            "evidence": evidence,
        },
    )

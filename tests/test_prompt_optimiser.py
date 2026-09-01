"""
Proposing a better planner prompt, and keeping it only if it wins.

The model that writes the candidates cannot be unit-tested, and does not need
to be: everything that decides what *sticks* is deterministic, and that is the
part which can quietly ratchet the champion around on noise.

The margin is the load-bearing rule. Measured today: two identical runs of the
same engine both scored 3/6 while flipping four of six cases. A promotion rule
that accepted a one-case lead would have called that progress three times over.
"""
from __future__ import annotations

import pytest

from sdk.agent.prompt_optimiser import (
    PROMOTION_MARGIN,
    Variant,
    choose_promotion,
    current_champion,
    gather_evidence,
    promote,
    validate_variant,
)
from sdk.agent.queue import GoalTreeDecomposer

CHAMPION = GoalTreeDecomposer._get_prompt_template()


def _ok_variant(body: str = "Plan it carefully. ") -> str:
    """
    Long enough to clear the truncation rule, which is measured against the
    real champion rather than a fixed size -- so the helper scales with it.
    """
    padded = body * max(1, len(CHAMPION) // max(1, len(body)))
    return f"{padded}\nGoal: {{user_goal}}\nTools:\n{{tool_catalogue}}\n"


# --- validation: never spend an evaluation on a prompt that cannot work -------

def test_a_reasonable_variant_is_accepted():
    assert validate_variant(_ok_variant(), CHAMPION) == ""


@pytest.mark.parametrize("template, expected", [
    ("", "empty"),
    ("   ", "empty"),
])
def test_an_empty_variant_is_refused(template, expected):
    assert expected in validate_variant(template, CHAMPION)


def test_a_variant_missing_the_goal_placeholder_is_refused():
    """Without it the planner decomposes nothing and every goal plans alike."""
    template = "Decompose the work using {tool_catalogue}. " * 40
    assert "{user_goal}" in validate_variant(template, CHAMPION)


def test_a_variant_missing_the_tool_catalogue_is_refused():
    """The planner was blind to the registry once already; not again."""
    template = "Decompose {user_goal} into steps. " * 40
    assert "{tool_catalogue}" in validate_variant(template, CHAMPION)


def test_the_champion_repeated_back_is_refused():
    assert "identical" in validate_variant(CHAMPION, CHAMPION)


def test_a_truncated_generation_is_refused():
    """
    A prompt that lost most of its body is usually a generation that stopped
    early, and evaluating it spends an hour learning that.
    """
    assert "truncated" in validate_variant("{user_goal} {tool_catalogue}", CHAMPION)


def test_unbalanced_braces_are_refused():
    template = _ok_variant() + " and then {oops"
    assert "brace" in validate_variant(template, CHAMPION)


def test_an_invented_placeholder_is_refused():
    """.format() would raise KeyError at planning time rather than here."""
    template = _ok_variant() + "\nExtra: {not_a_real_field}\n"
    assert "not_a_real_field" in validate_variant(template, CHAMPION)


# --- promotion: only on evidence, and only by a margin ------------------------

def _scored(score: float) -> Variant:
    return Variant(template=_ok_variant(), score=score, passed=int(score * 18), total=18)


def test_a_clear_winner_is_promoted():
    winner, reason = choose_promotion(0.56, [_scored(0.78)])
    assert winner is not None
    assert "beats the champion" in reason


def test_a_one_case_lead_is_not_a_result():
    """
    18 samples, one case is ~5.6% -- inside the run-to-run variance measured on
    this benchmark. Promoting on it moves the champion around on noise.
    """
    winner, reason = choose_promotion(0.72, [_scored(0.78)])
    assert winner is None
    assert "margin" in reason


def test_the_margin_is_the_stated_one():
    just_under = 0.50 + PROMOTION_MARGIN - 0.01
    just_over = 0.50 + PROMOTION_MARGIN + 0.01

    assert choose_promotion(0.50, [_scored(just_under)])[0] is None
    assert choose_promotion(0.50, [_scored(just_over)])[0] is not None


def test_a_losing_challenger_leaves_the_champion_alone():
    winner, reason = choose_promotion(0.78, [_scored(0.56)])
    assert winner is None
    assert "champion still leads" in reason


def test_the_best_of_several_is_the_one_considered():
    variants = [_scored(0.56), _scored(0.83), _scored(0.61)]
    winner, _ = choose_promotion(0.50, variants)
    assert winner is not None and winner.score == 0.83


def test_rejected_variants_are_never_promoted():
    """A variant that failed validation has no score worth trusting."""
    bad = Variant(template="x", score=1.0, rejected="missing {user_goal}")
    winner, reason = choose_promotion(0.10, [bad])
    assert winner is None
    assert "no variant produced a score" in reason


def test_a_round_with_nothing_evaluated_promotes_nothing():
    assert choose_promotion(0.5, [])[0] is None


# --- evidence: mutate from what actually went wrong ---------------------------

def test_evidence_is_empty_before_anything_has_been_learned(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    assert gather_evidence() == []


def test_evidence_comes_from_her_own_lessons(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    from sdk.agent.task_lessons import TaskOutcome, record_task_lesson
    from sdk.agent.node_staging import promotion_passes, run_verification_cycle

    monkeypatch.setenv("TALA_NODE_PROMOTION_MIN_MINUTES", "0")
    record_task_lesson(TaskOutcome(
        goal_text="Repair the payload decoder",
        phases=["a (tool): Tool 'code_apply_patch' executed.",
                "b (verify): gate_failed -- the import was unresolved"],
    ))
    for _ in range(promotion_passes()):
        run_verification_cycle()

    evidence = gather_evidence()
    assert any("import was unresolved" in item for item in evidence)


def test_recurring_obstacles_come_first_and_say_how_often(tmp_path, monkeypatch):
    """
    A prompt change should be aimed at the failure that keeps happening. The
    count is what distinguishes a recurring obstacle from a one-off, and it
    only exists because a lesson sharpens instead of being overwritten.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.setenv("TALA_NODE_PROMOTION_MIN_MINUTES", "0")
    from sdk.agent.task_lessons import TaskOutcome, record_task_lesson
    from sdk.agent.node_staging import promotion_passes, run_verification_cycle

    for _ in range(3):
        record_task_lesson(TaskOutcome(
            goal_text="Repair the payload decoder",
            phases=["a (tool): Tool 'x' executed.",
                    "b (verify): gate_failed -- the import was unresolved"],
        ))
    record_task_lesson(TaskOutcome(
        goal_text="Migrate the settings table",
        phases=["a (tool): Tool 'y' executed.",
                "b (verify): gate_failed -- a one-off hiccup"],
    ))
    for _ in range(promotion_passes()):
        run_verification_cycle()

    evidence = gather_evidence()
    assert evidence, "expected obstacles from the lessons"
    assert "seen 3x" in evidence[0], f"most-repeated should lead, got {evidence[0]!r}"


# --- the champion is data, and a promotion is reversible ----------------------

def test_the_champion_defaults_to_the_built_in(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    template, origin = current_champion()
    assert origin == "built-in"
    assert template == GoalTreeDecomposer._get_prompt_template()


def test_promotion_installs_the_challenger_and_keeps_what_it_replaced(tmp_path, monkeypatch):
    """
    Recovering from a bad promotion must not require the planner, because the
    thing promoted is what the planner plans with.
    """
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    from sdk.agent.parser import parse_file

    challenger = _ok_variant("A markedly different instruction. ")
    result = promote(challenger, score=0.78, beat=0.56, evidence=["the import was unresolved"])

    assert result.get("path"), f"promotion wrote nothing: {result}"
    state = parse_file(result["path"]).state_data or {}
    assert state["prompt_template"] == challenger
    assert state["previous_template"] == GoalTreeDecomposer._get_prompt_template()
    assert state["promoted_score"] == 0.78
    assert state["previous_score"] == 0.56
    assert state["evidence"] == ["the import was unresolved"]


def test_a_promoted_prompt_is_what_the_planner_then_uses(tmp_path, monkeypatch):
    """The whole point: promotion has to change how the next goal is planned."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    challenger = _ok_variant("Prefer creating a file before importing from it. ")
    promote(challenger, score=0.78, beat=0.56, evidence=[])

    assert GoalTreeDecomposer.active_prompt_template() == challenger

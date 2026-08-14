"""
Regression tests for strip_replan_wrapper.

This function shipped ineffective once. It was written against wrapper strings
collected from wiki node titles, all of which began with the marker, so an
anchored ``startswith`` check looked right. What the re-planner actually emits
carries the decomposer's own lead-in first -- "Implement solution: You were
trying to accomplish ..." -- so the check matched nothing and descriptions kept
nesting a layer per retry. The strings below are taken verbatim from a real run
rather than invented, which is the whole point.
"""

from __future__ import annotations

import pytest

from sdk.agent.goal_engine import strip_replan_wrapper


#: Straight from a benchmark run: three layers, each adding a lead-in, a
#: wrapper, and a trailing failure narration.
REAL_THREE_DEEP = (
    "Implement solution: You were trying to accomplish the following task: "
    "'Implement solution: You were trying to accomplish the following task: "
    "'Implement solution: Fix the ImportError in script.py to use the standard "
    "os module instead.'. However, the execution failed with the following "
    "error: 'Node synthesis-2b4c995f failed validation/criteria: LLM synthesis "
    "failed: pool is closed'. Please generate a single, concrete repair "
    "action.'. However, the execution failed with the following error: 'Node "
    "synthesis-73a2b22a failed'. Please generate a single, concrete repair "
    "action."
)


def test_unwraps_the_real_three_layer_nesting():
    out = strip_replan_wrapper(REAL_THREE_DEEP)
    assert "You were trying to accomplish" not in out, (
        f"wrapper survived unwrapping: {out[:120]!r}"
    )
    assert "However, the execution failed" not in out
    assert "Fix the ImportError in script.py" in out


def test_marker_is_found_when_not_at_position_zero():
    """The bug that made the first version a no-op."""
    wrapped = (
        "Implement solution: You were trying to accomplish the following task: "
        "'Add a retry to the uploader'"
    )
    assert strip_replan_wrapper(wrapped) == "Add a retry to the uploader"


def test_leading_quote_is_not_stranded():
    """Cutting at '. However,' removes the closing quote, not the opening one."""
    wrapped = (
        "You were trying to accomplish the following task: 'Add a retry'. "
        "However, the execution failed with the following error: 'boom'."
    )
    assert strip_replan_wrapper(wrapped) == "Add a retry"


@pytest.mark.parametrize(
    "clean",
    [
        "Fix the ImportError in script.py",
        "Implement solution: Add retry logic",
        "Run the generator with --count 5",
        "Modify the code to correct the addition logic",
    ],
)
def test_ordinary_descriptions_pass_through_untouched(clean):
    assert strip_replan_wrapper(clean) == clean


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_input_is_safe(empty):
    assert strip_replan_wrapper(empty) == ""


def test_unwrapping_is_bounded():
    """A pathological string must not spin -- depth is capped, not recursive."""
    pathological = "You were trying to accomplish the following task: '" * 200
    strip_replan_wrapper(pathological)  # must simply return

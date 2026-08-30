"""
The decomposition prompt as data.

It was a string literal in sdk/agent/queue.py, so varying it meant editing
source -- a self-edit, an approval, and a restart. Nothing unattended can do
that, which is why an optimiser could not exist: the thing it would search over
was not addressable at runtime.

The shape follows _load_workflow_spec, which had the same problem first: the
node is the single canonical source, and anything unreadable or invalid falls
back to the built-in with the reason logged. A prompt node that no longer
parses must never stop the planner from planning.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from sdk.agent.queue import GoalTreeDecomposer


def _write_prompt_node(template: str) -> None:
    """
    Authored through the real writer, not hand-rolled HTML.

    A hand-written article is not schema-valid -- the parser requires
    attributes it is easy to omit -- so building one by hand tests a node shape
    the system would never produce.
    """
    from sdk.agent.documentation_nodes import create_documentation_node

    create_documentation_node(
        node_id=GoalTreeDecomposer.PROMPT_NODE_ID,
        title="Planner Prompt",
        doc_target="planner-prompt",
        semantic_memory=(
            "The prompt the goal decomposer uses. Stored as data so it can be "
            "varied at runtime without editing source."
        ),
        state_extra={"prompt_template": template},
    )


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    return tmp_path / "nodes"


# --- the default, which is most runs -----------------------------------------

def test_no_node_means_the_built_in_prompt(nodes):
    assert GoalTreeDecomposer._authored_prompt() is None
    assert GoalTreeDecomposer.active_prompt_template() == GoalTreeDecomposer._get_prompt_template()


def test_the_built_in_prompt_satisfies_its_own_requirements():
    """If it did not, every authored prompt would be rejected against a standard the default fails."""
    built_in = GoalTreeDecomposer._get_prompt_template()
    for placeholder in GoalTreeDecomposer.REQUIRED_PROMPT_PLACEHOLDERS:
        assert placeholder in built_in


# --- an authored prompt is used ----------------------------------------------

def test_a_valid_authored_prompt_is_used(nodes):
    template = "Decompose {user_goal} using only {tool_catalogue}. Return JSON."
    _write_prompt_node(template)

    assert GoalTreeDecomposer._authored_prompt() == template
    assert GoalTreeDecomposer.active_prompt_template() == template


def test_the_prompt_hash_follows_the_prompt_actually_used(nodes):
    """
    The decomposition cache keys on this hash. If it kept hashing the built-in
    while an authored prompt was in use, a cached plan built by one prompt would
    be served for another -- and an optimiser would score variants against
    results none of them produced.
    """
    before = GoalTreeDecomposer._get_prompt_hash()
    _write_prompt_node("Plan {user_goal} with {tool_catalogue}.")
    after = GoalTreeDecomposer._get_prompt_hash()

    assert before != after


# --- invalid nodes fall back, loudly -----------------------------------------

@pytest.mark.parametrize("template, missing", [
    ("Decompose {user_goal} and stop.", "{tool_catalogue}"),
    ("Use {tool_catalogue} and stop.", "{user_goal}"),
])
def test_a_prompt_missing_a_placeholder_is_refused(nodes, template, missing, caplog):
    """
    A prompt without {user_goal} would ask the model to decompose nothing, and
    every goal would decompose identically -- a failure that looks like the
    planner being stupid rather than the prompt being broken.
    """
    _write_prompt_node(template)

    with caplog.at_level("WARNING"):
        assert GoalTreeDecomposer._authored_prompt() is None
    assert missing in caplog.text
    assert GoalTreeDecomposer.active_prompt_template() == GoalTreeDecomposer._get_prompt_template()


def test_a_valid_node_carrying_no_template_falls_back(nodes, caplog):
    """
    A schema-valid node that simply has no prompt in it -- distinct from one
    that fails to parse, and it must say which happened. Written through the
    real writer so the branch under test is the missing-key one rather than the
    parser rejecting hand-rolled markup.
    """
    from sdk.agent.documentation_nodes import create_documentation_node

    create_documentation_node(
        node_id=GoalTreeDecomposer.PROMPT_NODE_ID,
        title="Planner Prompt",
        doc_target="planner-prompt",
        semantic_memory="A node that forgot to carry the prompt it exists for.",
        state_extra={"something_else": True},
    )

    with caplog.at_level("WARNING"):
        assert GoalTreeDecomposer._authored_prompt() is None
    assert "prompt_template" in caplog.text
    assert "could not be read" not in caplog.text, "this node parsed; the message must not say otherwise"


def test_an_empty_template_falls_back(nodes):
    _write_prompt_node("   ")
    assert GoalTreeDecomposer._authored_prompt() is None


def test_unparseable_html_does_not_stop_the_planner(nodes, caplog):
    """Losing the authored prompt is survivable; losing the planner is not."""
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / f"{GoalTreeDecomposer.PROMPT_NODE_ID}.html").write_text(
        "<not even close to html-nn", encoding="utf-8"
    )
    assert GoalTreeDecomposer._authored_prompt() is None
    assert GoalTreeDecomposer.active_prompt_template() == GoalTreeDecomposer._get_prompt_template()

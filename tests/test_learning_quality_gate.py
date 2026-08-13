"""
tests/test_learning_quality_gate.py
===================================
Guards the gate in front of Tala's HTML-NN wiki.

Learning nodes are pages in the RAG-graph's store of record. Anything written
there becomes retrieval context permanently, so a junk node is not clutter --
it is future context poisoning, and it grounds the training corpus too.

Before this gate, of 17 auto-created learning nodes roughly three carried a
real lesson. The fixtures below are taken verbatim from the ones that were
actually on disk, so a regression here is measured against what really
happened rather than invented examples.
"""

from __future__ import annotations

import pytest

from sdk.agent.documentation_nodes import (
    assess_learning_quality,
    assess_skill_quality,
    create_learning_documentation_node,
)

#: synthesize_python_skill runs on every completed goal, so every junk skill
#: node already had a Python body. Fixtures carry code for that reason.
SYNTH_CODE = (
    "from sdk.agent.tools import register_tool\n"
    '@register_tool(name="x", description="y")\n'
    "async def x():\n    return 1\n"
)

# Verbatim from the seven junk skill nodes that were on disk.
REAL_JUNK_SKILLS = [
    "Fix the IndexError in index_test.py by safely handling the out-of-bounds access",
    "Fix the failing assertion in test_tax.py. DO NOT delete or comment out the assert",
    "Create a new reusable skill by writing a Python file at sdk/agent/skills/clean_broken_skills.py",
    "Plan a simple CLI calculator (add, subtract, multiply, divide)",
    "Workflow: Unnamed Workflow",
    "",
    "Improve Tala Core autonomous execution reliability through a",
]

BOILERPLATE = (
    "Tala distilled a reusable learning event from the completed goal "
    "'{}' with 4 phase(s) and tests status 'not run'."
)

# Verbatim from nodes that existed on disk.
REAL_JUNK = [
    ("Fix the failing assertion in test_tax.py. DO NOT delete or comment out the assert statement.",
     BOILERPLATE.format("Fix the failing assertion")),
    ("Create a new file named common.py with a Logger class. Then refactor both user.py and order.py.",
     BOILERPLATE.format("Create a new file")),
    ("Create a Python script named hello.py that simply prints 'hello world'.",
     BOILERPLATE.format("Create a Python script")),
    ("Plan a simple CLI calculator (add, subtract, multiply, divide)",
     BOILERPLATE.format("Plan a simple CLI calculator")),
    ("Fix the ImportError in script.py to use the standard 'os' module instead.",
     BOILERPLATE.format("Fix the ImportError")),
    ("*(Executing run_workspace_command",
     "Some output was produced by the command execution here and logged."),
    ("Workflow: Unnamed Workflow",
     "A workflow was executed and completed successfully with several steps."),
    ("", "Empty topic with an otherwise reasonable summary attached for testing."),
    ('the test will print "All tests passed"',
     "The suite ran and emitted its completion banner to stdout after finishing."),
    ("I haven't been given a target yet, and rather than fire blindly, I could run something",
     "Reflecting on the absence of a concrete objective before choosing an action to take next."),
    ("You were trying to accomplish the following task: 'Base the fix on the error'",
     "A restatement of the task that was assigned, echoed back with additional context."),
]

# Verbatim from the nodes worth keeping.
REAL_KEEPERS = [
    ("Self-improvement tool parameter discipline",
     "Two failure modes discovered during a live self-improvement session: "
     "self_improvement.execute_plan requires structured replacements or file_writes "
     "payloads, and tool call attribute values must never contain literal angle "
     "brackets because they break the tool XML parser and silently abort the call."),
    ("when writing tests, including pytest or unittest-based testing can help",
     "Including a runnable test harness alongside generated code catches shape "
     "errors immediately rather than at import time, and pytest discovery works "
     "without extra configuration when files are named test_*.py."),
]


@pytest.mark.parametrize("topic,summary", REAL_JUNK)
def test_real_junk_is_rejected(topic, summary):
    reason = assess_learning_quality(topic, summary)
    assert reason, f"expected rejection for {topic[:50]!r}"


@pytest.mark.parametrize("topic,summary", REAL_KEEPERS)
def test_real_lessons_are_kept(topic, summary):
    reason = assess_learning_quality(topic, summary)
    assert reason is None, f"wrongly rejected {topic[:50]!r}: {reason}"


def test_task_imperative_is_rejected_even_with_a_good_summary():
    """
    The dominant junk source was self_documenter minting a node for every
    completed goal, titled with the raw goal text. Completing a task is not
    learning something, however well the summary reads.
    """
    reason = assess_learning_quality(
        "Refactor the retrieval layer to use hybrid search",
        "A genuinely detailed summary describing what was done across several "
        "files with plenty of substance and specifics to clear the length bar.",
    )
    assert reason and "task instruction" in reason


def test_completion_boilerplate_is_rejected_even_with_a_good_topic():
    reason = assess_learning_quality(
        "Hybrid retrieval ranking discipline",
        BOILERPLATE.format("some goal"),
    )
    assert reason and "template" in reason


def test_narration_is_rejected_even_when_it_dodges_the_imperative_check():
    """
    A wiki page title is a concept, not a sentence pulled from mid-conversation.
    These slip past the imperative rule because they open with a pronoun.
    """
    reason = assess_learning_quality(
        "I haven't been given a target yet, so I could run diagnostics instead",
        "A long enough summary about deciding what to do next when no explicit "
        "objective has been provided by the operator during an idle period.",
    )
    assert reason and "narration" in reason


def test_dangling_conjunction_does_not_smuggle_junk_past_the_gate():
    """
    Topics are sliced out after a learning cue, so they routinely arrive as
    "that I should fix the failing assertion in test_tax.py". The leading
    "that" shifts the real first word out of position and made both the
    imperative and narration checks miss.
    """
    reason = assess_learning_quality(
        "that I should fix the failing assertion in test_tax.py first",
        "A summary with enough words in it to comfortably clear the substance "
        "threshold so that the length rule is not what does the rejecting here.",
    )
    assert reason, "leading conjunction must not bypass the gate"


def test_thin_content_is_rejected():
    assert assess_learning_quality("Coolant runs hot", "It runs hot.")


def test_details_count_toward_substance():
    """A terse summary backed by concrete detail lines is legitimate."""
    reason = assess_learning_quality(
        "Ollama GPU passthrough",
        "Two things matter.",
        details=[
            "ggml-cuda.dll must be present or Ollama silently falls back to CPU",
            "the torch cu121 index is stale and installs a mismatched torchvision",
        ],
    )
    assert reason is None


def test_skipped_proposal_returns_status_instead_of_raising(tmp_path, monkeypatch):
    """
    A low-value learning must never break the flow that produced it -- the
    caller gets a skip result, not an exception.
    """
    result = create_learning_documentation_node(
        topic="Fix the failing assertion in test_tax.py",
        summary=BOILERPLATE.format("Fix the failing assertion"),
    )
    assert result["status"] == "skipped"
    assert result["reason"]


def test_force_bypasses_the_gate_for_explicit_requests(monkeypatch):
    """
    When a human explicitly says "record this", honour it. The gate exists to
    filter automatic writes, not to overrule the user.
    """
    captured = {}

    import sdk.agent.documentation_nodes as dn

    def fake_create_documentation_node(**kwargs):
        captured.update(kwargs)
        return {"status": "created", "node_id": kwargs.get("node_id")}

    monkeypatch.setattr(dn, "create_documentation_node", fake_create_documentation_node)

    result = dn.create_learning_documentation_node(
        topic="Fix the failing assertion in test_tax.py",
        summary="short",
        force=True,
    )
    assert result["status"] == "created"
    assert captured, "force=True must reach the writer"


# ---------------------------------------------------------------------------
# Skill nodes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("goal", REAL_JUNK_SKILLS)
def test_real_junk_skills_are_rejected(goal):
    """
    Skill nodes are worse than junk learning nodes: their bodies are raw Python
    and the wiki is a retrieval source, so junk here hijacks responses. Asked to
    run a self-test, Tala retrieved one of these and reproduced its file
    contents verbatim instead of calling any tool.
    """
    assert assess_skill_quality(goal, ["a", "b"], SYNTH_CODE), (
        f"expected rejection for {goal[:50]!r}"
    )


def test_executable_code_alone_does_not_earn_a_skill_node():
    """
    THE trap this gate has to avoid. synthesize_python_skill runs on every
    completed goal and returns code regardless of quality, so all seven junk
    nodes had a Python body. A gate keying on code presence would pass all of
    them -- the goal text is the only signal that separates a capability from
    a one-off fix.
    """
    reason = assess_skill_quality(
        "Fix the IndexError in index_test.py", ["a", "b", "c"], SYNTH_CODE
    )
    assert reason and "one-off" in reason


def test_named_capabilities_still_become_skill_nodes():
    assert assess_skill_quality("Self-Check Discipline", ["verify", "report"]) is None
    assert (
        assess_skill_quality(
            "Establish a repeatable provider failover procedure",
            ["probe", "rank", "switch"],
            SYNTH_CODE,
        )
        is None
    )


def test_completed_goal_does_not_write_a_junk_skill_node(tmp_path, monkeypatch):
    """End to end: the documenter returns skipped and writes nothing."""
    from sdk.agent.self_documenter import SelfDocumenter

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    documenter = SelfDocumenter(str(tmp_path))
    result = documenter.document_completed_goal(
        goal_text="Fix the failing assertion in test_tax.py",
        phases_completed=["inspect", "patch"],
        task_id="t-1",
        executable_code=SYNTH_CODE,
    )

    assert result["status"] == "skipped"
    assert not result["skill_node_path"]
    assert not list(tmp_path.rglob("skill-*.html"))


def test_note_that_no_longer_mints_a_node():
    """
    'note that' is ordinary connective prose -- "note that the config lives in
    settings.json" is an aside, not a standing rule. It used to fire the
    learning-cue path on every explanation.
    """
    from python.harness.memory import _is_learning_event

    assert not _is_learning_event(
        {}, "assistant", "Note that the config lives in settings.json."
    )


def test_directive_cues_still_fire():
    from python.harness.memory import _is_learning_event

    assert _is_learning_event(
        {}, "assistant", "From now on, prefer local inference backends."
    )
    assert _is_learning_event({}, "assistant", "The rule is: never skip hooks.")

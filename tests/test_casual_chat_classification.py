"""
tests/test_casual_chat_classification.py
==========================================
Regression coverage for python.harness.server._is_casual_chat_prompt.

Caught live, traced end to end: a plain task request --
"Now write scratch/test_calculator.py exercising all four operations from
scratch/calculator.py, and run it to confirm everything passes." -- got
classified as casual/roleplay chat purely because the word "everything"
contains the substring "hi", which was one of the (plain substring-matched,
no word boundaries) casual-chat trigger terms. Casual chat prompts never
get tool-call documentation injected into the system prompt (small local
models otherwise echo the example call syntax verbatim), so this silently
left the model with no instructions on how to actually write/run a file --
it described the steps in prose and told the user to do it manually
instead, and even hallucinated wrong function names in the process.
"""
from python.harness.server import _is_casual_chat_prompt


def test_ordinary_task_request_is_not_casual_chat():
    # The exact reported prompt.
    assert _is_casual_chat_prompt(
        "Now write scratch/test_calculator.py exercising all four operations "
        "from scratch/calculator.py, and run it to confirm everything passes."
    ) is False


def test_html_nn_authoring_request_is_not_casual_chat():
    # A second, independently reported live instance of the same "hi" inside
    # "everything" false positive: this exact prompt got misclassified as
    # casual chat, which strips ALL registered tools (including
    # distill_workflow_to_skill / generate_detailed_neural_node, the tools
    # that actually author nodes/skill-*.html) out of the system prompt.
    # With no tool documentation and no way to know those tools existed,
    # the model fell back to writing plain source to a guessed filename
    # (calculator.md) instead of producing a real HTML-NN skill node.
    assert _is_casual_chat_prompt(
        "This needs to be changed a little. Make it, as an HTML-nn. That way "
        "Tala will have the skill to build software for herself as an "
        "HTML-NN since everything she does is based on that."
    ) is False


def test_words_containing_hi_as_a_substring_do_not_false_positive():
    """The specific confirmed regression: "hi" must not match inside "everything",
    "this", "which", "machine", "white", "while", "child", "history", etc."""
    for prompt in [
        "Please confirm everything passes.",
        "This is a normal request.",
        "Which file should I edit?",
        "Run this on the machine in white.",
        "While you're at it, check the child process.",
        "Give me the history of this file.",
    ]:
        assert _is_casual_chat_prompt(prompt) is False, f"false positive on: {prompt!r}"


def test_genuine_casual_greetings_still_classified_as_casual_chat():
    for prompt in [
        "hi there!",
        "Hey, how are you?",
        "Hello!",
        "I love this feature",
        "roleplay as a pirate captain",
        "hey honey",
    ]:
        assert _is_casual_chat_prompt(prompt) is True, f"missed genuine casual chat: {prompt!r}"


def test_empty_and_non_string_input():
    assert _is_casual_chat_prompt("") is False
    assert _is_casual_chat_prompt(None) is False

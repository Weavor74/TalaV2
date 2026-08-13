"""
tests/test_documentation_nl_routing.py
======================================
Regression coverage for python.harness.server._maybe_handle_documentation_nl.

Caught live, traced end to end: a long operational-verification prompt was
answered instantly with "Documentation disk verification complete. Created: 0
Updated: 0 Unchanged: 0 Errors: 0 Verified: 3 ...". No model was involved at
all. _maybe_handle_documentation_nl intercepts the prompt before inference and,
when it decides the user is asking about documentation, runs a documentation
audit and returns that output as the entire reply.

Its gate substring-matched a bare "doc", which is inside ordinary words --
"docker", "doctor", "dockyard". The prompt's only trigger was a single mention
of terminal_backends/docker_backend.py in a bullet about escaping. Combined
with the equally loose "verify"/"validate"/"check"/"prove" test, any message
containing one of those common words plus a word containing "doc" was silently
swallowed and never reached Tala.

This is the third instance of this exact bug class in this codebase -- see
test_casual_chat_classification.py for "hi" inside "everything", and the
tot-ideation fix for generic "plan"/"design" hijacking. The shared lesson is
that these intent gates must match on word boundaries, never bare substrings.

The multi-word phrases ("html-nn", "neural node", "program target") are still
substring-matched on purpose: they cannot collide with a single ordinary word.
"""
import pytest

from python.harness.server import _maybe_handle_documentation_nl


# Ordinary messages that happen to contain a "doc"-containing word plus one of
# the verify-ish trigger words. All of these must reach the model.
@pytest.mark.parametrize(
    "prompt",
    [
        # The exact trigger from the live incident: one filename in a bullet.
        "Verify the app is operational. Three cosmetic escapes remain in the "
        "timeout messages of terminal_backends/{docker_backend,local,ssh_backend}.py.",
        "can you check my docker setup",
        "i need to check with my doctor",
        "verify the docker_backend timeout messages",
        "check the dockyard before we sail",
    ],
)
def test_ordinary_doc_substring_words_are_not_documentation_requests(prompt):
    assert _maybe_handle_documentation_nl(prompt) is None


# Genuine documentation requests must still route. The fix tightens the gate;
# it must not close it.
@pytest.mark.parametrize(
    "prompt",
    [
        "verify the documentation",
        "check the docs",
        "validate the doc nodes",
        "please verify the document",
        "verify the html-nn nodes",
        "check the neural node schema",
    ],
)
def test_real_documentation_requests_still_route(prompt):
    assert _maybe_handle_documentation_nl(prompt) is not None


def test_slash_commands_are_never_intercepted():
    # Explicit commands own their own routing.
    assert _maybe_handle_documentation_nl("/verify-doc-nodes") is None

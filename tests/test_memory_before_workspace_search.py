"""
A question about the user goes to memory, not to the project's source code.

Found 2026-10-29 in a live memory test. Asked "What's the code word for my spare key?", Tala called search_workspace_files, which found a
line in tests/test_vault_nodes.py ("the spare key is under the third flowerpot"), and told Steven that was his code word: a unit-test
fixture passed off as something he had said. The correct tool was one call away (search_memory) and was on offer; the two descriptions
were neutral ("across workspace files" / "unified memory index"), so a model choosing by name picked the one called search-files.

Both tools are always on offer (DEFAULT_CORE_TOOLS), so ranking is not what picks between them: the model reads the descriptions, and they
now say what each is for and send personal questions to memory.
"""
from __future__ import annotations

import pytest

from python.harness.tool_selection import DEFAULT_CORE_TOOLS
from sdk.agent.tools import ToolRegistry


@pytest.fixture(scope="module")
def tools():
    return {t["name"]: t for t in ToolRegistry.get_tools()}


def test_the_workspace_search_says_it_reads_the_codebase_and_sends_personal_questions_to_memory(tools):
    text = tools["search_workspace_files"]["description"].lower()
    assert "project" in text and "codebase" in text
    assert "personal" in text, "it must say that personal questions are not its business"
    assert "search_memory" in text, "and name the tool that is"


def test_memory_search_says_it_is_where_facts_about_the_user_live_and_to_try_it_first(tools):
    text = tools["search_memory"]["description"].lower()
    assert "about the user" in text and "told" in text
    assert "first" in text
    assert "not" in text and "codebase" in text, "it must say it is not for searching the project"


def test_both_tools_are_always_offered_so_the_choice_is_the_models_to_make_well():
    assert "search_memory" in DEFAULT_CORE_TOOLS and "search_workspace_files" in DEFAULT_CORE_TOOLS

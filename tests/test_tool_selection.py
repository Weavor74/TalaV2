"""
Per-turn tool selection.

Every provider used to receive all 73 registry tools on every turn — ~5,975
prompt tokens, measured — which taxed every request and meant the registry could
not grow. Selection is a budget mechanism: the 4B chooses correctly among all 73,
it just cannot afford them.

Because a missed tool is a capability the model silently does not have, the
tests here are weighted toward the safety properties (a floor, stickiness,
degrading to everything on failure) rather than toward ranking quality.
"""
import pytest

from python.harness.tool_selection import (
    DEFAULT_BUDGETS,
    DEFAULT_CORE_TOOLS,
    ToolIndex,
    budget_for,
    conversation_text,
    select_specs,
    _select_tool_names,
)

TOOLS = [
    {"name": "web_search", "description": "Query prioritized web search APIs and return results."},
    {"name": "send_telegram_message", "description": "Send a text message to a Telegram chat using a bot token."},
    {"name": "run_workspace_command", "description": "Execute a shell command on the local machine and return output."},
    {"name": "read_workspace_file", "description": "Read the contents of a file from the workspace."},
    {"name": "search_workspace_files", "description": "Find files in the workspace matching a query."},
    {"name": "search_memory", "description": "Search the neural node memory graph."},
    {"name": "get_system_info", "description": "Report CPU, memory and operating system details."},
    {"name": "compile_lora_adapter", "description": "Compile active weight updates into a safetensors adapter binary."},
    {"name": "starcitizen_lookup", "description": "Look up Star Citizen ships, systems and galactapedia lore."},
]
NAMES = [t["name"] for t in TOOLS]


@pytest.fixture(scope="module")
def index():
    return ToolIndex(TOOLS)


def _extras(selected):
    """Selections beyond the always-on core — i.e. what the ranking contributed."""
    return [n for n in selected if n not in DEFAULT_CORE_TOOLS]


# --- ranking ----------------------------------------------------------------


def test_prompt_selects_the_obviously_matching_tool(index):
    selected = _select_tool_names("Send a telegram message to my group", index, 8, available=NAMES)

    assert "send_telegram_message" in selected


def test_domain_word_absent_from_every_description_still_lands(index):
    """No tool mentions docker. 'running' and 'machine' have to carry it — this
    is why an authored keyword map is a supplement and not the whole mechanism."""
    selected = _select_tool_names("Check if docker is running on this machine", index, 8, available=NAMES)

    assert "run_workspace_command" in selected


def test_conversational_turn_selects_nothing_beyond_the_core(index):
    """A turn that needs no tool should carry no tool payload — cheaper, and it
    removes the over-calling seen when every tool is always on offer."""
    assert _extras(_select_tool_names("How are you feeling today?", index, 8, available=NAMES)) == []


def test_name_matches_outrank_description_matches(index):
    """A tool called starcitizen_lookup is *about* Star Citizen; one that merely
    mentions ships in prose is not."""
    scores = index.score("star citizen ship lookup")

    assert max(scores, key=scores.get) == "starcitizen_lookup"


def test_common_terms_are_damped_not_counted_equally():
    """`node` appears in 14 live tools and `html` in 21. Undamped, one such word
    in a prompt selects the whole registry and the budget does nothing."""
    shared = [{"name": f"tool_{i}", "description": "operates on an html neural node"} for i in range(20)]
    shared.append({"name": "unique_widget", "description": "manages a widget"})
    idx = ToolIndex(shared)

    assert idx.score("widget")["unique_widget"] > idx.score("html node")["tool_0"]


# --- safety properties ------------------------------------------------------


def test_core_tools_are_always_offered(index):
    """The floor is what makes a crude ranker survivable: a miss costs quality,
    never the ability to look something up or read a file."""
    selected = _select_tool_names("something entirely unrelated to any tool", index, 8, available=NAMES)

    for core in DEFAULT_CORE_TOOLS:
        if core in NAMES:
            assert core in selected


def test_sticky_tools_survive_a_topic_change(index):
    """A task that searched on turn 1 and writes on turn 3 must not lose the
    search tool as the conversation drifts away from the original wording."""
    selected = _select_tool_names(
        "now compile it", index, 8, sticky={"starcitizen_lookup"}, available=NAMES,
    )

    assert "starcitizen_lookup" in selected


def test_ranked_fill_never_exceeds_the_budget(index):
    """The budget bounds what ranking contributes, on top of the guarantees."""
    for budget in (5, 8):
        selected = _select_tool_names("search files and send a message", index, budget, available=NAMES)
        assert len(selected) <= budget


def test_guarantees_win_over_the_budget_rather_than_being_truncated(index):
    """A budget below the core size returns the core. Trimming the floor to
    honour the ceiling would remove the very property that makes a keyword
    ranker safe to ship — the ceiling yields instead."""
    selected = _select_tool_names(
        "unrelated", index, 1, sticky={"compile_lora_adapter"}, available=NAMES,
    )

    assert "compile_lora_adapter" in selected
    for core in DEFAULT_CORE_TOOLS:
        if core in NAMES:
            assert core in selected


def test_zero_budget_means_unlimited(index):
    """Remote providers keep every tool, so their behaviour is untouched."""
    selected = _select_tool_names("anything", index, 0, available=NAMES)

    assert set(selected) == set(NAMES)


def test_selection_never_invents_a_tool_that_was_not_offered(index):
    selected = _select_tool_names("search the web", index, 8, available=["web_search"])

    assert selected == ["web_search"]


# --- spec filtering ---------------------------------------------------------


def _specs():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": {"type": "object", "properties": {}, "required": []}}}
            for t in TOOLS]


def test_withheld_tools_are_reported_not_silently_dropped(index):
    """A quietly reduced toolset is indistinguishable from a broken registry.
    The caller logs this list; returning it is what makes that possible."""
    kept, withheld = select_specs(_specs(), "send a telegram message", index, 6)

    assert len(kept) == 6
    assert withheld
    assert set(k["function"]["name"] for k in kept).isdisjoint(withheld)
    assert len(kept) + len(withheld) == len(TOOLS)


def test_unlimited_budget_returns_everything_and_withholds_nothing(index):
    kept, withheld = select_specs(_specs(), "anything", index, 0)

    assert len(kept) == len(TOOLS)
    assert withheld == []


# --- budgets ----------------------------------------------------------------


def test_remote_providers_are_unlimited_by_default():
    """Selection exists for the local VRAM ceiling; remote has neither the
    ceiling nor the reason to risk a miss."""
    assert budget_for("remote") == 0


def test_embedded_gets_the_smallest_budget():
    assert 0 < DEFAULT_BUDGETS["embedded"] < DEFAULT_BUDGETS["local"]


def test_provider_config_can_override_the_tier_default():
    assert budget_for("local", {"tool_budget": 3}) == 3
    assert budget_for("local", {"tool_budget": 0}) == 0
    assert budget_for("local", {"tool_budget": "nonsense"}) == DEFAULT_BUDGETS["local"]


# --- scoring input ----------------------------------------------------------


def test_system_messages_are_excluded_from_scoring_text():
    """The system prompt carries the persona and the tool documentation itself.
    Scoring against it would match nearly every tool and flatten the ranking."""
    text = conversation_text([
        {"role": "system", "content": "You are Tala. You may call web_search, read_workspace_file..."},
        {"role": "user", "content": "send a telegram message"},
    ])

    assert "telegram" in text
    assert "Tala" not in text


def test_recent_turns_are_included_so_selection_tracks_the_task():
    text = conversation_text([
        {"role": "user", "content": "find the parser"},
        {"role": "assistant", "content": "<call:search_workspace_files query=\"parser\" />"},
        {"role": "user", "content": "now read it"},
    ], turns=3)

    assert "find the parser" in text and "now read it" in text


def test_vision_turns_do_not_break_scoring():
    """Vision messages carry a content list of parts, not a string."""
    text = conversation_text([
        {"role": "user", "content": [{"type": "text", "text": "describe this screenshot"}]},
    ])

    assert "screenshot" in text

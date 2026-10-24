"""
A fact the user tells Tala becomes a real HTML-NN node.

``write_memory`` used to INSERT into ``memory_index (node_id, content, topic_tags)``, columns that table has never had
(it has ref_id, content_text, trust_label), so the tool failed on every call and no fact was ever saved. It also never
touched the HTML-NN store, which is where Tala's memory lives.

Now:

* ``write_memory`` (the model-initiated tool, lease-gated) writes an un-staged node, so the fact is recallable on the
  very next turn, seeded below the weight ceiling so use and feedback can move it;
* ``_remember_from_message`` is the deterministic path for an explicit "remember that ..." typed by the user: the harness
  saves that one fact itself, before the model answers, instead of hoping a small model chooses to call a tool;
* neither stores anything that looks like a credential, a wallet key or a seed phrase.
"""
from __future__ import annotations

import pytest

from sdk.agent import node_learning as nl
from sdk.agent.memory_tools import _remember_from_message, write_memory
from sdk.agent.parser import parse_file


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    (tmp_path / "nodes").mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_LEARNING", raising=False)
    monkeypatch.setattr(nl, "_halted", lambda: False)
    monkeypatch.setattr(nl, "_ANCHOR_RUNNER", lambda fn: fn())
    monkeypatch.setattr("sdk.agent.anchor.canonicalize_and_anchor", lambda **kw: ("h", "id", "anchored", "", None))
    return tmp_path


FACT = "my dog is named Biscuit and he is afraid of thunder"


@pytest.mark.asyncio
async def test_a_fact_becomes_a_real_node_that_parses(userdata):
    result = await write_memory(FACT, "pets")
    assert result["success"] is True and result["created"] is True
    path = userdata / "nodes" / f"{result['node_id']}.html"
    assert path.is_file() and path.parent.name == "nodes", "written to the live nodes folder, not a staging area"
    node = parse_file(str(path))
    assert FACT in node.semantic_memory
    assert node.node_id.startswith("fact-")
    assert max(node.execution_authority, node.memory_authority) > 0.0, "it must be allowed to be recalled"


@pytest.mark.asyncio
async def test_the_new_node_starts_below_the_ceiling_with_a_usage_clock(userdata):
    result = await write_memory(FACT, "pets")
    node = parse_file(result["path"])
    assert (node.weight_relevance, node.weight_confidence) == (0.8, 0.8)
    assert node.usage_count == 0 and node.last_used is not None


@pytest.mark.asyncio
async def test_telling_her_the_same_fact_twice_does_not_duplicate_or_reset_it(userdata):
    first = await write_memory(FACT, "pets")
    nl.record_use([first["node_id"]], userdata / "nodes")
    again = await write_memory(FACT, "pets")
    assert again["node_id"] == first["node_id"] and again["created"] is False
    node = parse_file(first["path"])
    assert node.usage_count == 1 and node.weight_relevance == pytest.approx(0.82), "learning was not reset"
    assert len(list((userdata / "nodes").glob("fact-*.html"))) == 1


@pytest.mark.asyncio
async def test_with_learning_off_the_node_is_still_saved_at_the_default_weights(userdata, monkeypatch):
    monkeypatch.setenv("TALA_LEARNING", "0")
    result = await write_memory(FACT, "pets")
    node = parse_file(result["path"])
    assert result["success"] is True and (node.weight_relevance, node.weight_confidence) == (1.0, 1.0)
    assert node.usage_count == 0 and node.last_used is None


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [
    "my password is hunter2xyz",
    "my api key is 8f3a-not-a-real-key",
    "my seed phrase is correct horse battery staple",
    "here is my private key for the wallet",
    "my access token: not-a-real-token-value",
    "my social security number is 123-45-6789",
])
async def test_nothing_that_looks_like_a_credential_is_stored(userdata, secret):
    result = await write_memory(secret, "private")
    assert result["success"] is False and "credential" in result["error"].lower()
    assert list((userdata / "nodes").glob("*.html")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "   ", "x" * 2001])
async def test_empty_and_oversized_facts_are_refused(userdata, content):
    result = await write_memory(content)
    assert result["success"] is False
    assert list((userdata / "nodes").glob("*.html")) == []


# --- the deterministic path: the user explicitly asked ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("message,fact", [
    ("Remember that my sister's birthday is March 3rd.", "my sister's birthday is March 3rd."),
    ("please remember: the garage door sticks in winter", "the garage door sticks in winter"),
    ("Tala, remember my favourite colour is teal", "my favourite colour is teal"),
    ("remember that I take my coffee black", "I take my coffee black"),
])
async def test_an_explicit_remember_is_saved_before_the_model_answers(userdata, message, fact):
    result = await _remember_from_message(message)
    assert result is not None and result["success"] is True
    assert fact in parse_file(result["path"]).semantic_memory


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "What do you remember about my sister?",
    "I remember that day at the lake",
    "remember when we went to the coast?",
    "Do you remember that thing I said?",
    "remember it",
    "Remember to turn off the stove before you leave the house tonight.",
    "Remember to output valid JSON only, with no commentary before or after it.",
    "remember that " + "the build instructions are long and keep going " * 20,
    "",
    None,
])
async def test_only_an_explicit_command_counts(userdata, message):
    assert await _remember_from_message(message) is None
    assert list((userdata / "nodes").glob("*.html")) == []


@pytest.mark.asyncio
async def test_an_explicit_remember_of_a_secret_is_refused_not_ignored(userdata):
    result = await _remember_from_message("remember that my password is hunter2xyz")
    assert result is not None and result["success"] is False
    assert list((userdata / "nodes").glob("*.html")) == []


@pytest.mark.asyncio
async def test_remember_safely_never_lets_a_failure_reach_the_turn(monkeypatch):
    """The server calls this before every turn; a problem saving a note must cost the note, not the reply."""
    from sdk.agent import memory_tools

    async def boom(message):
        raise RuntimeError("disk full")

    monkeypatch.setattr(memory_tools, "_remember_from_message", boom)
    assert await memory_tools.remember_safely("remember that my cat is called Zorblax") is None


@pytest.mark.asyncio
async def test_remember_safely_passes_a_saved_note_through(monkeypatch):
    from sdk.agent import memory_tools

    async def saved(message):
        return {"success": True, "node_id": "fact-x"}

    monkeypatch.setattr(memory_tools, "_remember_from_message", saved)
    assert (await memory_tools.remember_safely("remember that x"))["node_id"] == "fact-x"

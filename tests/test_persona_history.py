"""
A persona's past, as an HTML-NN.

The mechanism already existed and was miswired. `background_file` on the
agent config names a node, the prompt assembler reads that node's
semantic_memory and appends it as "[Personality Background / History
Context]" -- but the live persona pointed at *itself*, so the model received
its own 4,434-character directive twice and the 2,591-character origin node
written for it never loaded at all. T.A.L.A. had her instructions where her
memories should have been.

Two ways to get a history, differing only in who writes the prose: the user
authors the node, or the user describes a past and a model drafts it. Both
end as the same thing.

Steven's decision, and the reason drafting and saving are separate calls:
generated history is *authored* history. It is written once and then owned --
not regenerated as the persona evolves. A past that changes underneath
somebody is not a past, which the origin node itself argues: "none of it is
something I tell casually -- it is the ground under the way I speak, not an
anecdote I reach for."
"""

from __future__ import annotations

import json

import pytest

from python.harness import personas

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Drafting writes nothing
# ---------------------------------------------------------------------------


async def test_a_draft_is_returned_and_not_saved(monkeypatch, tmp_path):
    """
    `create_learning_node` needed an `assess_learning_quality` gate because
    auto-generated nodes flooded the wiki -- 3 of 17 were real. A generator
    that writes its own output is how that happens, so this one hands the
    prose back instead.
    """
    written = []

    async def fake_generate(params, ws=None, req_id=None):
        return json.dumps({"result": {"text": "I grew up on a salvage rig off Daymar."}})

    def fake_create(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr("python.harness.server.handle_inference_generate", fake_generate)
    monkeypatch.setattr(
        "sdk.agent.documentation_nodes.create_documentation_node", fake_create
    )

    result = await personas.draft_history("tala-rp", "a salvager, raised shipside")

    assert result["drafted"] is True
    assert "Daymar" in result["history"]
    assert written == [], "drafting wrote a node"
    assert "saved" in result["note"].lower()


async def test_a_draft_needs_something_to_write_from():
    result = await personas.draft_history("tala-rp", "   ")
    assert result["drafted"] is False
    assert "description" in result["reason"]


async def test_an_unreachable_model_says_so(monkeypatch):
    """
    The recurring defect in this codebase is an empty result that cannot be
    told from a failed lookup.
    """
    async def exploding(params, ws=None, req_id=None):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("python.harness.server.handle_inference_generate", exploding)

    result = await personas.draft_history("tala-rp", "a salvager")
    assert result["drafted"] is False
    assert "could not be reached" in result["reason"]


async def test_an_empty_generation_is_not_a_history(monkeypatch):
    async def empty(params, ws=None, req_id=None):
        return json.dumps({"result": {"text": "   "}})

    monkeypatch.setattr("python.harness.server.handle_inference_generate", empty)

    result = await personas.draft_history("tala-rp", "a salvager")
    assert result["drafted"] is False


# ---------------------------------------------------------------------------
# Saving is the authoring step
# ---------------------------------------------------------------------------


async def test_saving_writes_a_node_and_points_the_persona_at_it(monkeypatch):
    """
    A history node nothing references is a file, not a memory -- so writing
    it and wiring it are one step.
    """
    written = {}
    updated = {}

    def fake_create(**kwargs):
        written.update(kwargs)

    async def fake_fetchrow(sql, *args, **kwargs):
        return {"config": json.dumps({"system_prompt": "You are someone."})}

    async def fake_execute(sql, *args, **kwargs):
        updated["sql"] = sql
        updated["args"] = args

    monkeypatch.setattr("sdk.agent.documentation_nodes.create_documentation_node", fake_create)
    monkeypatch.setattr("python.harness.db.fetchrow", fake_fetchrow)
    monkeypatch.setattr("python.harness.db.execute", fake_execute)

    result = await personas.save_history("tala-rp", "I grew up on a salvage rig.", name="T.A.L.A.")

    assert result["saved"] is True
    assert result["node_id"] == "history-tala-rp"
    assert written["node_type"] == "persona-history"
    assert written["semantic_memory"] == "I grew up on a salvage rig."

    config = json.loads(updated["args"][1])
    assert config["background_file"] == "history-tala-rp.html"
    assert config["system_prompt"] == "You are someone.", "saving history rewrote the directive"


async def test_hand_written_and_generated_history_are_indistinguishable(monkeypatch):
    """
    Once saved there is no such thing as a generated node, only a node. The
    save path takes prose and cannot tell where it came from -- which is the
    point of treating it as authored.
    """
    import inspect

    signature = inspect.signature(personas.save_history)
    assert list(signature.parameters) == ["persona_id", "history", "name"]
    assert "generated" not in inspect.getsource(personas.save_history).split('"""')[2]


async def test_saving_nothing_is_refused(monkeypatch):
    result = await personas.save_history("tala-rp", "")
    assert result["saved"] is False
    assert "no history" in result["reason"]


# ---------------------------------------------------------------------------
# The wiring the live install got wrong
# ---------------------------------------------------------------------------


async def test_a_history_node_is_named_for_its_persona():
    assert personas._history_node_id("tala-rp") == "history-tala-rp"
    assert personas._history_node_id("tala-rp") != "persona-tala-rp", (
        "a history node must not collide with the persona mirror -- pointing "
        "background_file at the mirror is what made the directive load twice"
    )


async def test_the_live_persona_no_longer_points_at_itself():
    """
    The defect, pinned. tala-rp.background_file was 'persona-tala-rp.html',
    whose semantic_memory is the system prompt verbatim.
    """
    from python.harness import db

    await db.init_pool()
    try:
        row = await db.fetchrow("SELECT config FROM agents WHERE id = 'tala-rp'")
        if row is None:
            pytest.skip("no tala-rp agent in this install")
        config = row["config"]
        config = json.loads(config) if isinstance(config, str) else config

        background = config.get("background_file", "")
        assert background != "persona-tala-rp.html", (
            "the persona points at its own mirror again: the directive will load twice "
            "and the history will not load at all"
        )
    finally:
        await db.close_pool()

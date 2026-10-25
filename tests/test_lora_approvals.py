"""
The review queue in front of the LoRA corpus: nothing is trained on unless Steven approved it.

Candidates are her chat replies (with the message they answered) and her HTML-NN skills and memories. Anything the
exclude-never-redact filters reject is listed as blocked and can never be approved; a decision belongs to one exact piece of
content (the id is a hash of it), so editing a node or a reply voids the approval rather than carrying it over.
The database is faked, the nodes directory and user-data root are tmp_path; the safety filters run for real.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sdk.agent import lora_approvals as la
from sdk.agent import real_usage_corpus as ruc

WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CLEAN_REPLY = "I kept the cargo manifest in order, and I would rather tell you the route is risky than pretend it is not."
SECOND_REPLY = "Quiet nights on the Nyx run are when I notice how much I like the work, even the boring parts of it."

NODE = """<!DOCTYPE html><html><body>
<article data-node-id="{node_id}" data-node-type="{node_type}" data-doc-target="{target}" data-document-purpose="record"
         data-generated-at="2026-09-30T10:00:00+00:00">
  <header><h1>{title}</h1></header>
  <section id="semantic-memory"><p>{text}</p></section>
  {extra}
</article></body></html>"""


def _row(reply, prompt="How was the run tonight, Tala?", prev_role="user", created_at=WHEN, content=None):
    return {"content": content if content is not None else reply, "tool_calls": [], "created_at": created_at,
            "prev_role": prev_role, "prev_content": prompt}


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    from sdk.agent import documentation_nodes

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    monkeypatch.setattr(documentation_nodes, "resolve_active_nodes_dir", lambda: nodes)
    from python.harness import db

    state = {"rows": []}

    async def fake_fetch(query, *args):
        return state["rows"]

    monkeypatch.setattr(db, "fetch", fake_fetch)
    return type("World", (), {"rows": state, "nodes": nodes, "tmp": tmp_path})


def write_node(world, name, *, text, node_type="skill", target="skill.test", title="A skill", extra=""):
    (world.nodes / name).write_text(NODE.format(node_id=name, node_type=node_type, target=target, title=title, text=text, extra=extra),
                                    encoding="utf-8")


# --- what becomes a candidate ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chat_reply_is_a_candidate_with_the_message_it_answered(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]

    items = (await la.list_candidates())["items"]

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "chat" and item["reply"] == CLEAN_REPLY and item["prompt"] == "How was the run tonight, Tala?"
    assert item["blocked"] is None and item["decision"] == "pending"


@pytest.mark.asyncio
async def test_what_the_filters_reject_is_listed_as_blocked_with_a_reason(_world):
    _world.rows["rows"] = [
        _row("(Executing run_command now) the result will follow shortly, please hold on"),
        _row("my key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD so you can use it for the deploy step later"),
        _row(CLEAN_REPLY, prev_role="tool"),
        _row(CLEAN_REPLY),
        _row(CLEAN_REPLY, prompt="Same reply again?"),
    ]

    items = (await la.list_candidates())["items"]
    reasons = sorted((i["blocked"] or "ok") for i in items)

    assert reasons == ["duplicate", "no user message", "ok", "safety filter", "tool narration"]


@pytest.mark.asyncio
async def test_a_message_that_fails_the_safety_filter_blocks_the_pair(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY, prompt="here is my password: hunter2hunter2 please remember it for later")]

    [item] = (await la.list_candidates())["items"]

    assert item["blocked"] == "safety filter (message)"


@pytest.mark.asyncio
async def test_her_skills_and_memories_are_candidates_but_documentation_and_secured_nodes_are_not(_world):
    write_node(_world, "skill-a.html", text="When the docker daemon is down, check the WSL distro before restarting anything.",
               title="Docker is down")
    write_node(_world, "memory-b.html", text="Steven prefers the quiet hours for long runs and dislikes surprises in the schedule.",
               node_type="memory", target="memory.fact", title="Steven's schedule")
    write_node(_world, "doc-c.html", text="Module documentation that is generated, not remembered by anyone at all.",
               node_type="documentation", target="program-doc.module", title="Docs")
    write_node(_world, "secret-d.html", text="A private thing that must never be encoded into any weights at all, ever.",
               target="skill.secret", title="Secret",
               extra='<script type="application/json" id="node-metadata">{"access_level": "SECURED"}</script>')

    items = (await la.list_candidates(kind="memory"))["items"]

    by_title = {i["title"]: i for i in items}
    assert set(by_title) == {"Docker is down", "Steven's schedule", "Secret"}
    assert by_title["Secret"]["blocked"] == "secured"
    assert by_title["Docker is down"]["blocked"] is None
    assert by_title["Docker is down"]["prompt"] and "Docker is down" in by_title["Docker is down"]["prompt"]


@pytest.mark.asyncio
async def test_an_id_belongs_to_exact_content(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    first = (await la.list_candidates())["items"][0]["id"]
    _world.rows["rows"] = [_row(CLEAN_REPLY + " Edited.")]
    second = (await la.list_candidates())["items"][0]["id"]
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    again = (await la.list_candidates())["items"][0]["id"]

    assert first != second and first == again


# --- deciding -------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approving_persists_and_the_queue_reflects_it(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY), _row(SECOND_REPLY, prompt="And the quiet nights?")]
    ids = [i["id"] for i in (await la.list_candidates())["items"]]

    result = await la.decide([ids[0]], "approved")

    assert result == {"updated": 1, "refused": {}}
    listing = await la.list_candidates()
    assert listing["counts"] == {"pending": 1, "approved": 1, "rejected": 0, "blocked": 0}
    assert [i["id"] for i in (await la.list_candidates(status="approved"))["items"]] == [ids[0]]
    assert [i["id"] for i in (await la.list_candidates(status="pending"))["items"]] == [ids[1]]


@pytest.mark.asyncio
async def test_a_blocked_item_cannot_be_approved(_world):
    _world.rows["rows"] = [_row("my key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD so you can use it for the deploy step later")]
    [item] = (await la.list_candidates())["items"]

    result = await la.decide([item["id"]], "approved")

    assert result["updated"] == 0 and result["refused"] == {item["id"]: "safety filter"}
    assert await la.approved_items() == []


@pytest.mark.asyncio
async def test_an_unknown_id_and_a_bad_decision_are_refused(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]

    assert (await la.decide(["nope"], "approved"))["refused"] == {"nope": "no such item"}
    with pytest.raises(ValueError):
        await la.decide(["nope"], "maybe")


@pytest.mark.asyncio
async def test_pending_clears_a_decision_and_rejected_is_remembered(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    [item] = (await la.list_candidates())["items"]

    await la.decide([item["id"]], "approved")
    await la.decide([item["id"]], "rejected")
    assert (await la.list_candidates())["counts"]["rejected"] == 1 and await la.approved_items() == []
    await la.decide([item["id"]], "pending")
    assert (await la.list_candidates())["counts"]["pending"] == 1


@pytest.mark.asyncio
async def test_the_decisions_are_an_html_nn_record_that_survives_a_restart(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    [item] = (await la.list_candidates())["items"]
    await la.decide([item["id"]], "approved")

    stored = la._store_path().read_text(encoding="utf-8")

    assert "<article" in stored and 'data-doc-target="system.lora-approvals"' in stored
    assert item["id"] in stored and 'data-decision="approved"' in stored
    assert la._read_decisions()[item["id"]]["decision"] == "approved"


# --- what the trainer is given --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_approved_items_reach_the_dataset(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY), _row(SECOND_REPLY, prompt="And the quiet nights?")]
    ids = [i["id"] for i in (await la.list_candidates())["items"]]
    await la.decide([ids[1]], "approved")

    approved = await la.approved_items()

    assert [a["id"] for a in approved] == [ids[1]]
    assert approved[0]["reply"] == SECOND_REPLY and approved[0]["prompt"] == "And the quiet nights?"


@pytest.mark.asyncio
async def test_the_safety_filter_is_run_again_when_the_dataset_is_built(_world, monkeypatch):
    """Approval is not trusted past the filter: if the rules tighten (or a node was swapped under the same id), it is dropped."""
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    [item] = (await la.list_candidates())["items"]
    await la.decide([item["id"]], "approved")

    monkeypatch.setattr(ruc, "_passes_safety_filter", lambda text: False)

    assert await la.approved_items() == []


@pytest.mark.asyncio
async def test_an_approval_for_content_that_no_longer_exists_does_nothing(_world):
    _world.rows["rows"] = [_row(CLEAN_REPLY)]
    [item] = (await la.list_candidates())["items"]
    await la.decide([item["id"]], "approved")
    _world.rows["rows"] = []

    assert await la.approved_items() == []


@pytest.mark.asyncio
async def test_listing_pages_and_filters_by_kind(_world):
    _world.rows["rows"] = [_row(f"Reply number {n} is its own distinct and harmless sentence about the cargo run.") for n in range(5)]
    write_node(_world, "skill-a.html", text="When the docker daemon is down, check the WSL distro before restarting anything.")

    page = await la.list_candidates(kind="chat", limit=2, offset=1)

    assert len(page["items"]) == 2 and page["total"] == 5
    assert {i["kind"] for i in (await la.list_candidates(kind="memory"))["items"]} == {"memory"}

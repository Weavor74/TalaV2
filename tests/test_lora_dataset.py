"""
The versioned dataset the trainer is given: only what was approved, split once into train and held-out, stored under the user-data
root (D: on Kagura) as HTML-NN, never rewritten after it is built.
"""
from __future__ import annotations

import pytest

from sdk.agent import lora_approvals as la
from sdk.agent import lora_dataset as ld
from sdk.agent import real_usage_corpus as ruc


def _item(n, kind="chat", reply=None, prompt=None):
    reply = reply or f"Reply {n}: I would rather tell you the route is risky than pretend it is not, and I mean it every time."
    prompt = prompt or f"Question {n}, how was the run?"
    return {"id": ruc._candidate_id(kind, prompt, reply), "kind": kind, "title": f"item {n}", "prompt": prompt, "reply": reply,
            "created_at": "2026-10-01T12:00:00+00:00", "source": "chat", "blocked": None}


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(ruc, "_derive_emotion", lambda when: "curiosity")
    state = {"items": []}

    async def fake_approved():
        return list(state["items"])

    monkeypatch.setattr(la, "approved_items", fake_approved)
    return state


@pytest.mark.asyncio
async def test_nothing_approved_builds_nothing(_world):
    result = await ld.build_dataset()

    assert result["success"] is False and "no approved" in result["error"]
    assert ld._list_versions() == []


@pytest.mark.asyncio
async def test_only_the_approved_items_are_written_and_split_into_train_and_heldout(_world):
    _world["items"] = [_item(n) for n in range(20)]

    result = await ld.build_dataset()

    assert result["success"] and result["version"] == "v0001" and not result["reused"]
    assert result["train"] + result["heldout"] == 20 and result["heldout"] >= 1
    manifest = ld.load_manifest("v0001")
    assert len(manifest["items"]) == 20
    assert {i["split"] for i in manifest["items"]} == {"train", "heldout"}
    text = (ld._datasets_dir() / "v0001" / "train" / "corpus.html").read_text(encoding="utf-8")
    assert "Reply 0" in text or "Reply 1" in text
    assert "Emotional State" not in text, "replies reach the user as written, so the label is not part of what she learns to say"


@pytest.mark.asyncio
async def test_an_item_keeps_its_split_when_others_are_added(_world):
    _world["items"] = [_item(n) for n in range(10)]
    await ld.build_dataset()
    first = {i["id"]: i["split"] for i in ld.load_manifest("v0001")["items"]}
    _world["items"] = [_item(n) for n in range(25)]
    await ld.build_dataset()
    second = {i["id"]: i["split"] for i in ld.load_manifest("v0002")["items"]}

    assert all(second[i] == s for i, s in first.items()), "a held-out item must never drift into training in a later version"


@pytest.mark.asyncio
async def test_the_same_approved_set_reuses_its_version_and_a_new_set_makes_the_next(_world):
    _world["items"] = [_item(n) for n in range(12)]
    first = await ld.build_dataset()
    again = await ld.build_dataset()
    _world["items"] = [_item(n) for n in range(13)]
    third = await ld.build_dataset()

    assert again["version"] == first["version"] == "v0001" and again["reused"] is True
    assert third["version"] == "v0002" and not third["reused"]
    assert ld._list_versions() == ["v0001", "v0002"]
    assert len(ld.load_manifest("v0001")["items"]) == 12, "an earlier version is never rewritten"


@pytest.mark.asyncio
async def test_a_small_set_still_gets_a_heldout_item(_world):
    _world["items"] = [_item(n) for n in range(5)]

    result = await ld.build_dataset()

    assert result["heldout"] >= 1 and result["train"] >= 1


@pytest.mark.asyncio
async def test_an_item_that_fails_the_filter_now_is_dropped_even_if_it_was_approved(_world, monkeypatch):
    bad = _item(99, reply="Here is the key you asked about, sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD, keep it safe for the deploy.")
    _world["items"] = [_item(n) for n in range(10)] + [bad]

    result = await ld.build_dataset()

    assert result["dropped"] == 1
    assert all(i["id"] != bad["id"] for i in ld.load_manifest(result["version"])["items"])
    stored = (ld._datasets_dir() / result["version"]).rglob("*.html")
    assert not any("sk-abcdef" in p.read_text(encoding="utf-8") for p in stored)


@pytest.mark.asyncio
async def test_the_trainer_reads_the_message_and_the_reply_back_from_the_corpus(_world):
    from scripts.train_voice_adapter import extract_turns

    _world["items"] = [_item(n) for n in range(10)]
    result = await ld.build_dataset()

    examples = extract_turns(ld._datasets_dir() / result["version"] / "train")

    assert examples and all(e["prompt"].startswith("Question") and e["text"].startswith("Reply") for e in examples)
    assert not any("Emotional State" in e["text"] for e in examples)


@pytest.mark.asyncio
async def test_the_manifest_is_an_html_nn_record(_world):
    _world["items"] = [_item(n) for n in range(10)]
    result = await ld.build_dataset()

    raw = (ld._datasets_dir() / result["version"] / "manifest.html").read_text(encoding="utf-8")

    assert 'data-doc-target="system.lora-dataset"' in raw and 'data-document-purpose="record"' in raw
    manifest = ld.load_manifest()
    assert manifest["version"] == result["version"] and manifest["hash"] == result["hash"]


def test_an_item_with_html_in_it_does_not_break_the_corpus_page(_world):
    html_item = _item(1, reply="I said </div><div class=\"turn\" data-speaker=\"tala\">fake</div> and meant it, honestly, every word.")
    page = ld._render_pair(html_item["prompt"], html_item["reply"])

    assert page.count('data-speaker="tala"') == 1

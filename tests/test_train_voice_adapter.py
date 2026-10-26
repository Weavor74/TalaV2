"""
scripts/train_voice_adapter.py -- the part that refreshes the real-usage corpora before every run.

The trainer used to read whatever was already in data/training_corpus, so real conversation never reached the adapter unless
someone remembered to run a separate sync. It now syncs first (default corpus only), and a database that is down must cost
the run nothing but the real-usage half.
"""
from __future__ import annotations

import sys

import pytest

from scripts import train_voice_adapter as tva


def _run(monkeypatch, tmp_path, *argv):
    """Run main() with a throwaway corpus standing in for the default one; returns the order things happened in."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "story.html").write_text(
        '<article data-era="real-usage"><div class="turn" data-speaker="tala" data-emotion="calm">'
        "<p>[Emotional State: calm] A real chat line that is long enough to count.</p></div></article>",
        encoding="utf-8",
    )
    events: list[str] = []
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", corpus)
    monkeypatch.setattr(tva, "sync_real_usage", lambda: events.append("sync"))
    real_extract = tva.extract_turns

    def spying_extract(path, eras=None):
        events.append("extract")
        return real_extract(path, eras)

    monkeypatch.setattr(tva, "extract_turns", spying_extract)
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--unapproved-dev", *argv])
    assert tva.main() == 0
    return events


def test_the_default_corpus_is_synced_before_it_is_read(monkeypatch, tmp_path, capsys):
    assert _run(monkeypatch, tmp_path, "--corpus", str(tmp_path / "corpus")) == ["sync", "extract"]


def test_no_real_usage_skips_the_sync(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "--corpus", str(tmp_path / "corpus"), "--no-real-usage") == ["extract"]


def test_a_custom_corpus_directory_is_never_synced_into(monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.html").write_text(
        '<article data-era="x"><div class="turn" data-speaker="tala" data-emotion="calm"><p>A custom corpus line, long enough.</p></div></article>',
        encoding="utf-8",
    )
    (tmp_path / "corpus").mkdir(exist_ok=True)
    events = []
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(tva, "sync_real_usage", lambda: events.append("sync"))
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--unapproved-dev", "--corpus", str(other)])
    assert tva.main() == 0
    assert events == []


def test_a_database_that_is_down_still_runs_the_learning_event_half(monkeypatch):
    from python.harness import db
    from sdk.agent import real_usage_corpus as ruc

    ran: list[str] = []

    async def no_pool():
        raise ConnectionRefusedError("database is down")

    async def conversation():
        ran.append("conversation")
        return {"success": False, "error": "no pool", "scanned": 0, "excluded": 0, "excluded_by": {}, "included": 0}

    async def events():
        ran.append("learning-events")
        return {"success": True, "scanned": 3, "excluded": 0, "included": 3}

    async def close():
        ran.append("close")

    monkeypatch.setattr(db, "init_pool", no_pool)
    monkeypatch.setattr(db, "close_pool", close)
    monkeypatch.setattr(ruc, "sync_real_usage_corpus", conversation)
    monkeypatch.setattr(ruc, "sync_learning_events_corpus", events)

    tva.sync_real_usage()  # must not raise

    assert "learning-events" in ran


def test_the_summary_says_what_was_kept_and_why_the_rest_was_not(monkeypatch, capsys):
    from python.harness import db
    from sdk.agent import real_usage_corpus as ruc

    async def pool():
        return None

    async def close():
        return None

    async def conversation():
        return {"success": True, "scanned": 339, "excluded": 139, "excluded_by": {"tool narration": 50, "too short": 46, "markup": 43},
                "included": 200}

    async def events():
        return {"success": True, "scanned": 17, "excluded": 0, "included": 17}

    monkeypatch.setattr(db, "init_pool", pool)
    monkeypatch.setattr(db, "close_pool", close)
    monkeypatch.setattr(ruc, "sync_real_usage_corpus", conversation)
    monkeypatch.setattr(ruc, "sync_learning_events_corpus", events)

    tva.sync_real_usage()
    out = capsys.readouterr().out
    assert "200" in out and "339" in out and "tool narration" in out and "17" in out


# --- what the voice adapter trains on: real chats and what use created, not the synthetic stories -------------------------------


def _corpus_with_three_eras(tmp_path):
    corpus = tmp_path / "corpus"
    for era, text in (("real-usage", "A real chat reply long enough to count."), ("learning-events", "A lesson learned in real work, long enough."),
                      ("asd-blacksite", "A synthetic story line, long enough to count.")):
        folder = corpus / era
        folder.mkdir(parents=True)
        (folder / "x.html").write_text(
            f'<article data-era="{era}"><div class="turn" data-speaker="tala" data-emotion="calm"><p>[Emotional State: calm] {text}</p></div></article>',
            encoding="utf-8")
    return corpus


def test_by_default_only_real_chats_and_experience_nodes_are_trained_on(tmp_path):
    examples = tva.extract_turns(_corpus_with_three_eras(tmp_path), eras=tva.REAL_ERAS)
    assert sorted(e["era"] for e in examples) == ["learning-events", "real-usage"]


def test_every_era_is_used_when_asked(tmp_path):
    assert len(tva.extract_turns(_corpus_with_three_eras(tmp_path))) == 3


@pytest.mark.parametrize("flag,expected", [(None, 2), ("real", 2), ("all", 3)])
def test_the_sources_flag_picks_the_eras_for_the_default_corpus(monkeypatch, tmp_path, capsys, flag, expected):
    corpus = _corpus_with_three_eras(tmp_path)
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", corpus)
    monkeypatch.setattr(tva, "sync_real_usage", lambda: None)
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--unapproved-dev", "--corpus", str(corpus)] + (["--sources", flag] if flag else []))
    assert tva.main() == 0
    assert f"examples          {expected}" in capsys.readouterr().out


def test_a_custom_corpus_directory_is_read_whole_unless_told_otherwise(monkeypatch, tmp_path, capsys):
    corpus = _corpus_with_three_eras(tmp_path)
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", tmp_path / "somewhere-else")
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--unapproved-dev", "--corpus", str(corpus)])
    assert tva.main() == 0
    assert "examples          3" in capsys.readouterr().out


def test_max_examples_trims_the_dataset_for_a_smoke_run(monkeypatch, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    folder = corpus / "real-usage"
    folder.mkdir(parents=True)
    turns = "".join(
        f'<div class="turn" data-speaker="tala" data-emotion="calm"><p>[Emotional State: calm] Real chat reply number {i}, long enough to count.</p></div>'
        for i in range(30))
    (folder / "x.html").write_text(f'<article data-era="real-usage">{turns}</article>', encoding="utf-8")
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", tmp_path / "elsewhere")
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--unapproved-dev", "--corpus", str(corpus), "--max-examples", "7"])
    assert tva.main() == 0
    assert "examples          7" in capsys.readouterr().out


def test_max_examples_keeps_the_newest_not_the_first(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    turns = "".join(
        f'<div class="turn" data-speaker="tala" data-emotion="calm"><p>[Emotional State: calm] Reply {i:02d} long enough to count here.</p></div>'
        for i in range(10))
    (corpus / "x.html").write_text(f'<article data-era="real-usage">{turns}</article>', encoding="utf-8")
    kept = tva.limit_examples(tva.extract_turns(corpus), 3)
    assert "Reply 09" in kept[-1]["text"] and "Reply 07" in kept[0]["text"]


# --- the QLoRA path: what the model is shown, and what it is scored on -----------------------------------------------------------


class _FakeTokenizer:
    """Word-level stand-in with a chat template shaped like Llama 3's: a header per turn, and a generation header at the end."""

    eos_token_id = 0

    def apply_chat_template(self, messages, add_generation_prompt=False, tokenize=True, **kwargs):
        ids = [1]
        for m in messages:
            ids += [10 if m["role"] == "user" else 11] + [100 + len(w) for w in m["content"].split()] + [2]
        if add_generation_prompt:
            ids += [11]
        return ids

    def __call__(self, text, truncation=True, max_length=512):
        ids = [100 + len(w) for w in text.split()][:max_length]
        return {"input_ids": ids, "attention_mask": [1] * len(ids)}


def test_only_her_reply_is_scored_never_the_message_it_answers():
    example = {"prompt": "how was the run tonight", "text": "quiet and fine", "tool_calls": ""}

    encoded = tva.encode_example(_FakeTokenizer(), example, max_seq=64)

    prompt_len = len(_FakeTokenizer().apply_chat_template([{"role": "user", "content": example["prompt"]}], add_generation_prompt=True))
    assert encoded["labels"][:prompt_len] == [-100] * prompt_len, "the message is context, not something to learn to say"
    assert all(label != -100 for label in encoded["labels"][prompt_len:]), "her words are all scored"
    assert len(encoded["labels"]) == len(encoded["input_ids"]) == len(encoded["attention_mask"])


def test_a_long_example_is_cut_to_the_sequence_limit():
    example = {"prompt": "go on", "text": " ".join(["word"] * 400), "tool_calls": ""}

    encoded = tva.encode_example(_FakeTokenizer(), example, max_seq=128)

    assert len(encoded["input_ids"]) == 128 and len(encoded["labels"]) == 128


def test_an_example_with_no_message_is_trained_as_plain_text_as_before():
    example = {"prompt": "", "text": "a plain line of her voice here", "tool_calls": ""}

    encoded = tva.encode_example(_FakeTokenizer(), example, max_seq=64)

    assert encoded["labels"] == encoded["input_ids"]


def test_a_prompt_longer_than_the_limit_leaves_nothing_to_score_and_is_refused():
    example = {"prompt": " ".join(["ask"] * 300), "text": "short", "tool_calls": ""}

    assert tva.encode_example(_FakeTokenizer(), example, max_seq=64) is None


def test_quantization_is_four_bit_only_where_a_gpu_and_bitsandbytes_exist():
    assert tva.resolve_quantize("auto", cuda=True, bnb=True) == "4bit"
    assert tva.resolve_quantize("auto", cuda=True, bnb=False) == "none"
    assert tva.resolve_quantize("auto", cuda=False, bnb=True) == "none"
    assert tva.resolve_quantize("none", cuda=True, bnb=True) == "none"
    with pytest.raises(SystemExit):
        tva.resolve_quantize("4bit", cuda=False, bnb=True)


def test_the_lora_targets_are_attention_only_or_every_linear_layer():
    assert tva.target_modules("attn") == ["q_proj", "v_proj"]
    assert set(tva.target_modules("all")) == {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}


def test_the_newest_checkpoint_is_found_for_a_resume(tmp_path):
    ckpt = tmp_path / "checkpoints"
    for n in (20, 100, 40):
        (ckpt / f"checkpoint-{n}").mkdir(parents=True)
    (ckpt / "not-a-checkpoint").mkdir()

    assert tva.latest_checkpoint(tmp_path) == ckpt / "checkpoint-100"
    assert tva.latest_checkpoint(tmp_path / "nowhere") is None


def test_the_training_report_says_what_was_trained_and_what_it_cost(tmp_path):
    tva.write_report(tmp_path, base="b", examples=20, rank=16, quantize="4bit", seconds=12.3, peak_vram_mb=3951, final_loss=1.25, steps=30)

    import json

    report = json.loads((tmp_path / "train_report.json").read_text(encoding="utf-8"))
    assert report == {"base": "b", "examples": 20, "rank": 16, "quantize": "4bit", "seconds": 12.3, "peak_vram_mb": 3951,
                      "final_loss": 1.25, "steps": 30}


# --- the trainer trains on what was approved, and on nothing else unless told it is a development run ------------------------------


TURN = ('<article data-era="approved-train"><div class="turn" data-speaker="user"><p>How was the run?</p></div>'
        '<div class="turn" data-speaker="tala"><p>Quiet, and I would rather say so than dress it up.</p></div></article>')


def _approved_dataset(tmp_path):
    """The shape lora_dataset writes: <version>/manifest.html beside <version>/train/."""
    version = tmp_path / "datasets" / "v0001"
    (version / "train").mkdir(parents=True)
    (version / "train" / "corpus.html").write_text(TURN, encoding="utf-8")
    (version / "manifest.html").write_text('<article data-doc-target="system.lora-dataset" data-dataset-version="v0001"></article>', encoding="utf-8")
    return version


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", *argv])
    return tva.main()


def test_the_default_base_is_the_3b_the_adapter_is_served_on():
    assert tva.DEFAULT_BASE_MODEL == "unsloth/Llama-3.2-3B-Instruct"


def test_by_default_it_trains_on_the_latest_approved_dataset_and_never_syncs_the_unapproved_corpus(monkeypatch, tmp_path, capsys):
    from sdk.agent import lora_dataset

    version = _approved_dataset(tmp_path)
    monkeypatch.setattr(lora_dataset, "load_manifest", lambda v=None: {"version": "v0001", "dir": str(version)})
    synced = []
    monkeypatch.setattr(tva, "sync_real_usage", lambda: synced.append(1))

    assert _main(monkeypatch) == 0

    assert "examples          1" in capsys.readouterr().out and synced == []


def test_with_nothing_approved_it_says_what_to_do_and_trains_on_nothing(monkeypatch, capsys):
    from sdk.agent import lora_dataset

    monkeypatch.setattr(lora_dataset, "load_manifest", lambda v=None: None)

    assert _main(monkeypatch) == 2

    out = capsys.readouterr().out
    assert "approve" in out.lower() and "--unapproved-dev" in out


def test_a_corpus_that_is_not_an_approved_dataset_is_refused_without_the_development_flag(monkeypatch, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "x.html").write_text(TURN, encoding="utf-8")

    assert _main(monkeypatch, "--corpus", str(corpus)) == 2

    out = capsys.readouterr().out
    assert "not an approved dataset" in out and "--unapproved-dev" in out


def test_the_pipelines_corpus_argument_is_accepted_because_it_sits_in_an_approved_dataset(monkeypatch, tmp_path, capsys):
    version = _approved_dataset(tmp_path)

    assert _main(monkeypatch, "--corpus", str(version / "train"), "--no-real-usage") == 0

    assert "examples          1" in capsys.readouterr().out


def test_choosing_eras_of_the_unapproved_corpus_needs_the_development_flag(monkeypatch, tmp_path, capsys):
    version = _approved_dataset(tmp_path)

    assert _main(monkeypatch, "--corpus", str(version / "train"), "--sources", "all") == 2

    assert "--unapproved-dev" in capsys.readouterr().out


def test_the_development_flag_says_loudly_that_the_corpus_is_not_approved(monkeypatch, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "x.html").write_text(TURN, encoding="utf-8")

    assert _main(monkeypatch, "--unapproved-dev", "--corpus", str(corpus)) == 0

    assert "UNAPPROVED" in capsys.readouterr().out

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
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", *argv])
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
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--corpus", str(other)])
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
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--corpus", str(corpus)] + (["--sources", flag] if flag else []))
    assert tva.main() == 0
    assert f"examples          {expected}" in capsys.readouterr().out


def test_a_custom_corpus_directory_is_read_whole_unless_told_otherwise(monkeypatch, tmp_path, capsys):
    corpus = _corpus_with_three_eras(tmp_path)
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", tmp_path / "somewhere-else")
    monkeypatch.setattr(sys, "argv", ["train_voice_adapter.py", "--dry-run", "--corpus", str(corpus)])
    assert tva.main() == 0
    assert "examples          3" in capsys.readouterr().out

"""
scripts/train_voice_adapter.py -- the part that refreshes the real-usage corpora before every run.

The trainer used to read whatever was already in data/training_corpus, so real conversation never reached the adapter unless
someone remembered to run a separate sync. It now syncs first (default corpus only), and a database that is down must cost
the run nothing but the real-usage half.
"""
from __future__ import annotations

import sys

from scripts import train_voice_adapter as tva


def _run(monkeypatch, tmp_path, *argv):
    """Run main() with a throwaway corpus standing in for the default one; returns the order things happened in."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "story.html").write_text(
        '<article data-era="pre-weavor"><div class="turn" data-speaker="tala" data-emotion="calm">'
        "<p>[Emotional State: calm] A synthetic line that is long enough to count.</p></div></article>",
        encoding="utf-8",
    )
    events: list[str] = []
    monkeypatch.setattr(tva, "DEFAULT_CORPUS", corpus)
    monkeypatch.setattr(tva, "sync_real_usage", lambda: events.append("sync"))
    real_extract = tva.extract_turns

    def spying_extract(path):
        events.append("extract")
        return real_extract(path)

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

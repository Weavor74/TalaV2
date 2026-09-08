"""
Documentation that knows when the code moved out from under it.

Steven's requirement, in his words:

    "Tala is supposed to use the html-nn docs to understand her code. Her
    personality and her structure so that she herself can modify them or so
    that she can explain how she works to someone. So if a bit of code gets
    updated, lets say from using a function on amazon to using an API key or
    something similar the documents need to be rewritten for that."

That could not happen, and the reason was one line. `_source_fingerprint`
hashes the file NAMES a document covers, its generated prose, and its target
— never the contents of the files. Swap an Amazon call for an API key and
the paths are identical and the stored prose is identical, so the fingerprint
is identical. Nothing anywhere could tell that the code had changed, and the
document went on describing the old shape with full confidence. Worse, it
was stored under the name ``source_fingerprint``, so it read as though that
question had already been answered.

`verify_documentation_nodes` did not close the gap either: it checks that a
node is well-FORMED — schema version, node type, authority values — and a
document describing code from June passes every one of those and reports as
verified.

So there are now two hashes with two jobs, and these tests keep them apart:

    _source_fingerprint   identifies the DOCUMENT; decides whether a
                          regeneration needs writing to disk
    _source_digest        identifies the CODE the document describes;
                          answers "has this gone stale"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import (
    _source_digest,
    documentation_staleness,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The digest has to be about the code
# ---------------------------------------------------------------------------


def test_the_digest_changes_when_the_content_does(tmp_path, monkeypatch):
    """
    The property the old fingerprint lacked, and the whole reason for this.
    """
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "_REPO_ROOT", tmp_path)
    source = tmp_path / "thing.py"

    source.write_text("def fetch():\n    return call_amazon()\n", encoding="utf-8")
    before = _source_digest(["thing.py"])

    source.write_text("def fetch():\n    return call_with_api_key()\n", encoding="utf-8")
    after = _source_digest(["thing.py"])

    assert before != after, (
        "the code changed from an Amazon call to an API key and the digest did not move"
    )


def test_the_digest_is_stable_when_nothing_changed(tmp_path, monkeypatch):
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "_REPO_ROOT", tmp_path)
    (tmp_path / "thing.py").write_text("x = 1\n", encoding="utf-8")
    assert _source_digest(["thing.py"]) == _source_digest(["thing.py"])


def test_file_order_is_not_a_change(tmp_path, monkeypatch):
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "_REPO_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    assert _source_digest(["a.py", "b.py"]) == _source_digest(["b.py", "a.py"])


def test_a_deleted_source_counts_as_a_change(tmp_path, monkeypatch):
    """
    A file disappearing is the most drastic edit there is. Skipping what
    cannot be read would make it the one edit nothing noticed.
    """
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "_REPO_ROOT", tmp_path)
    source = tmp_path / "thing.py"
    source.write_text("x = 1\n", encoding="utf-8")
    before = _source_digest(["thing.py"])

    source.unlink()
    assert _source_digest(["thing.py"]) != before


def test_the_two_hashes_are_not_the_same_thing():
    """
    They answer different questions, and conflating them is what left the
    store with 209 documents that could not be checked against anything.
    """
    from sdk.agent.documentation_nodes import _source_fingerprint

    files = ["python/harness/conversations.py"]
    prose = "some description"
    assert _source_fingerprint(files, prose, "t") != _source_digest(files)

    # The fingerprint moves when the DOCUMENT changes and the code has not.
    assert (_source_fingerprint(files, prose, "t")
            != _source_fingerprint(files, prose + " more", "t"))


# ---------------------------------------------------------------------------
# Staleness, against the real node store
# ---------------------------------------------------------------------------


def test_module_documentation_is_current():
    """
    The state the daily pass is supposed to maintain. If this fails, some
    module was edited and its document was not rewritten.
    """
    report = documentation_staleness()
    assert report["current"] > 100, (
        f"only {report['current']} documents can be checked against their source"
    )
    assert not report["stale"], (
        "documents describing code that has since changed: "
        + ", ".join(s["node_id"] for s in report["stale"])
        + ". Run: python scripts/generate_sdk_agent_nodes.py"
    )


def test_the_harness_is_documented_module_by_module():
    """
    It was not. There was a node for each of the 53 SDK modules and, for the
    seventeen-thousand-line harness that runs everything, one hand-written
    brief saying harness documentation "should explain method dispatch".
    """
    nodes = ROOT / "nodes"
    harness_nodes = list(nodes.glob("python_harness_*.html"))
    assert len(harness_nodes) > 40, (
        f"only {len(harness_nodes)} harness modules are documented"
    )
    for expected in ("python_harness_server", "python_harness_conversations",
                     "python_harness_vps_mode", "python_harness_ws_auth"):
        assert (nodes / f"{expected}.html").exists(), f"{expected} is undocumented"


def test_unknown_is_not_reported_as_current():
    """
    Most nodes predate the digest. Counting them as current would be a guess
    wearing the clothes of a measurement — which is the failure this whole
    change is about.
    """
    report = documentation_staleness()
    assert "unknown" in report
    # `records` joined these buckets when purpose became explicit metadata: a
    # benchmark run is neither current nor unknown, it is out of scope, and
    # every node has to land in exactly one bucket or the numbers mean nothing.
    assert (
        report["current"] + len(report["unknown"]) + len(report["stale"])
        + len(report["source_missing"]) + len(report["records"])
    ) == report["checked"]
    assert "coverage gap" in report["note"]


def test_a_documented_module_actually_describes_itself():
    """
    A document is only worth checking if reading it tells you something. The
    program-doc briefs did not: for the whole harness, one sentence saying
    what documentation ought to cover.
    """
    import html as html_mod
    import re

    node = (ROOT / "nodes" / "python_harness_conversations.html").read_text(
        encoding="utf-8", errors="replace"
    )
    body = re.search(r'<section id="semantic-memory">(.*?)</section>', node, re.DOTALL)
    assert body
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", body.group(1)))

    assert "python/harness/conversations.py" in text
    for symbol in ("create", "post", "participants", "set_membership"):
        assert symbol in text, f"the document does not mention {symbol}()"
    assert "should explain" not in text, (
        "this is a brief about what documentation ought to cover, not documentation"
    )


@pytest.mark.asyncio
async def test_the_refresh_rewrites_what_it_finds(tmp_path):
    """
    Detection alone leaves the reader with a warning and no answer. The pass
    has to rewrite, and it must be able to say what it could not.
    """
    from python.harness import doc_maintenance

    target = ROOT / "python" / "harness" / "conversations.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# an edit\n")
        result = await doc_maintenance.refresh_stale_documentation()
        assert result["ran"] is True
        assert result["stale"] >= 1
        assert "python_harness_conversations" in result["rewritten_nodes"]
    finally:
        target.write_bytes(original)
        await doc_maintenance.refresh_stale_documentation()

    assert not documentation_staleness()["stale"]

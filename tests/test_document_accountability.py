"""
Which way accountability runs between a document and the code.

Purpose says whether freshness applies. Accountability says what a change
MEANS — and they are different questions that a single "documentation" label
could not tell apart. The reviewer drew the line:

    "Schema and authoring standards: identify the document as normative. Link
    the parser, validator and conformance tests as implementations accountable
    to that specification. A code change should trigger a conformance review,
    not automatically rewrite the standard."

That inversion is the point. When `parser.py` changes, the schema node has not
gone stale. Rewriting it to match would let an implementation bug redefine the
rule it violated — the document would agree with the code precisely when the
code had stopped obeying the document, which is the one moment the
disagreement was worth having.

Three measures are also kept apart here, because collapsing them is how a
coverage figure gets read as an assurance it never measured:

    source coverage          does the document name what it answers to
    source freshness         has that thing changed since it was written
    behavioural verification does the document describe what the code does

The third is not automated at all, and says so.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import (
    ACCOUNTABILITIES,
    ACCOUNTABILITY_CONCEPTUAL,
    ACCOUNTABILITY_DESCRIPTIVE,
    ACCOUNTABILITY_NORMATIVE,
    accountability_of,
    documentation_staleness,
    resolve_active_nodes_dir,
)

ROOT = Path(__file__).resolve().parents[1]


def _attr(node_id: str, name: str) -> str:
    html = (resolve_active_nodes_dir() / f"{node_id}.html").read_text(
        encoding="utf-8", errors="replace"
    )
    match = re.search(rf'{name}="([^"]*)"', html)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_the_three_directions_exist():
    assert set(ACCOUNTABILITIES) == {"normative", "descriptive", "conceptual"}


def test_descriptive_is_the_safe_default():
    """
    A document that answers to the code shows a mismatch as its own
    staleness. Defaulting the other way would make an unclassified document
    silently claim the code was wrong.
    """
    assert accountability_of("") == ACCOUNTABILITY_DESCRIPTIVE
    assert accountability_of("nonsense") == ACCOUNTABILITY_DESCRIPTIVE
    assert accountability_of("normative") == ACCOUNTABILITY_NORMATIVE


# ---------------------------------------------------------------------------
# Normative: the code answers to the document
# ---------------------------------------------------------------------------


def test_the_schema_is_normative_and_names_its_implementations():
    assert _attr("system-neural-node-v2-schema", "data-document-accountability") == "normative"
    conforming = _attr("system-neural-node-v2-schema", "data-conforming-implementations")
    assert "sdk/agent/parser.py" in conforming
    assert "sdk/agent/validator.py" in conforming


@pytest.mark.asyncio
async def test_an_implementation_change_asks_for_review_not_a_rewrite():
    """
    The inversion, end to end. Change the parser and the schema must not be
    reported stale, must not be regenerated, and must raise a conformance
    question instead.
    """
    parser = ROOT / "sdk" / "agent" / "parser.py"
    node = resolve_active_nodes_dir() / "system-neural-node-v2-schema.html"
    original = parser.read_bytes()
    before = node.read_text(encoding="utf-8", errors="replace")

    from python.harness.doc_maintenance import refresh_stale_documentation

    try:
        parser.write_bytes(original + b"\n# an implementation change\n")

        report = documentation_staleness()
        due = {c["node_id"] for c in report["conformance_review_due"]}
        stale = {s["node_id"] for s in report["stale"]}

        assert "system-neural-node-v2-schema" in due, (
            "changing the parser raised no conformance question against the schema"
        )
        assert "system-neural-node-v2-schema" not in stale, (
            "the standard was reported stale; a standard does not go stale "
            "because something stopped conforming to it"
        )

        await refresh_stale_documentation()
        assert node.read_text(encoding="utf-8", errors="replace") == before, (
            "the maintenance pass rewrote a standard to match the code that "
            "changed under it — an implementation bug could redefine the rule "
            "it violated"
        )
    finally:
        parser.write_bytes(original)


def test_the_conformance_message_says_which_way_to_look():
    """
    "Something changed" is not actionable. Which artefact is under suspicion
    is the whole content of the finding.
    """
    parser = ROOT / "sdk" / "agent" / "parser.py"
    original = parser.read_bytes()
    try:
        parser.write_bytes(original + b"\n# an implementation change\n")
        report = documentation_staleness()
        entry = next(c for c in report["conformance_review_due"]
                     if c["node_id"] == "system-neural-node-v2-schema")
        assert "review it against this standard" in entry["why"]
        assert "rather than rewriting the standard" in entry["why"]
    finally:
        parser.write_bytes(original)


# ---------------------------------------------------------------------------
# Conceptual: a scope and an owner instead of files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_id", [
    "doc-self-improvement-discipline",
    "widget_api_bridge_v1",
])
def test_conceptual_documents_have_an_owner_and_no_invented_sources(node_id):
    """
    The reviewer was explicit: do not attach arbitrary source files merely to
    improve coverage. A conceptual document with a file bolted on reads as
    accountable to something it is not.
    """
    assert _attr(node_id, "data-document-accountability") == ACCOUNTABILITY_CONCEPTUAL
    assert _attr(node_id, "data-review-owner"), f"{node_id} has no review owner"
    assert not _attr(node_id, "data-source-files"), (
        f"{node_id} is conceptual and has source files attached"
    )


def test_conceptual_documents_are_excluded_rather_than_counted_as_a_gap():
    report = documentation_staleness()
    conceptual = {c["node_id"] for c in report["conceptual"]}
    unknown = {u["node_id"] for u in report["unknown"]}
    assert conceptual, "no conceptual documents were recognised"
    assert not (conceptual & unknown)
    assert report["source_coverage"]["excluded_conceptual"] == len(conceptual)


def test_a_learning_record_filed_under_a_module_prefix_is_corrected():
    """
    `sdk_agent_web_browser_skill` opens with "Durable learning event captured
    by Tala" and was classified as SDK module documentation by its prefix.
    The declaration has to beat the inference, or the family rule quietly
    decides what things are.
    """
    assert _attr("sdk_agent_web_browser_skill", "data-document-purpose") == "record"


# ---------------------------------------------------------------------------
# Three measures, kept apart
# ---------------------------------------------------------------------------


def test_the_report_separates_coverage_from_freshness_from_verification():
    report = documentation_staleness()
    for key in ("source_coverage", "source_freshness", "behavioural_verification"):
        assert key in report, f"{key} is not reported separately"
        assert report[key].get("measures") or report[key].get("note")


def test_behavioural_verification_does_not_claim_to_be_measured():
    """
    The one that would be most tempting to imply. Nothing measures whether a
    description matches what the code does; source coverage is a different
    question and must not be read as this one.
    """
    verification = documentation_staleness()["behavioural_verification"]
    assert verification["verified"] == 0
    assert "not verification" in verification["note"]


def test_every_accountable_document_names_what_it_answers_to():
    """
    The milestone: not zero unknowns overall — records and conceptual
    documents are supposed to be outside this — but every document that
    claims accountability naming the thing it is accountable to.
    """
    coverage = documentation_staleness()["source_coverage"]
    assert coverage["without_reference"] == 0, (
        f"{coverage['without_reference']} accountable document(s) name nothing"
    )
    assert coverage["with_reference"] == coverage["accountable_documents"]


@pytest.mark.parametrize("node_id,expected", [
    ("system-voice-adapter-training", "descriptive"),
    ("system-ollama-adapter-deployment", "descriptive"),
])
def test_documents_naming_a_real_pipeline_are_descriptive(node_id, expected):
    """
    Both of these were first classified conceptual on a reading of their
    prose, and both already named implementation files that exist. Naming a
    pipeline that is implemented is answering to it — the evidence overruled
    the first reading, which is what a classification review is for.
    """
    assert _attr(node_id, "data-document-accountability") == expected
    assert _attr(node_id, "data-source-files"), f"{node_id} names nothing"


def test_dangling_references_were_cleared_not_preserved():
    """
    Two documents pointed at files that no longer exist —
    skill-self-check-discipline.html and widgets/external_api_stub.py. A
    conceptual document holding a dangling reference is worse than one
    holding none: it looks accountable to something, and the something is
    gone.
    """
    for node_id in ("doc-self-improvement-discipline", "widget_api_bridge_v1"):
        assert not _attr(node_id, "data-source-files"), (
            f"{node_id} still names a file it cannot answer to"
        )

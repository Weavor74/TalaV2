"""
What a document is FOR decides whether freshness applies to it.

The reviewer's distinction, which the staleness report could not previously
express:

    historical records   benchmarks, lessons, execution evidence — preserve
                         the content and provenance; never regenerate against
                         current code
    current descriptions architecture, operations, UI, tools — require source
                         references, freshness checks and a refresh path
    mixed                separate the historical evidence from the current
                         description

Treating both kinds as "documentation" made the second look like the first.
A benchmark run from August is not stale; it is a record of what happened in
August, and regenerating it against today's code would destroy the evidence.
But it sat in the same undifferentiated "unknown" pile as a description of
the tool registry that genuinely nobody could check — 139 of them, a number
that could not say which of them anybody should do anything about.

So purpose is now explicit metadata, and the number that matters is coverage
of current descriptions. Eliminating every "unknown" was never the goal:
records are *supposed* to be outside freshness checking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import (
    DOCUMENT_PURPOSES,
    PURPOSE_DESCRIPTION,
    PURPOSE_RECORD,
    documentation_staleness,
    purpose_of,
    resolve_active_nodes_dir,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


def test_records_are_recognised_by_family():
    for node_id in ("benchmark-run-20260829-174755",
                    "learning-something-observed",
                    "goal-abc123"):
        assert purpose_of(node_id) == PURPOSE_RECORD, node_id


def test_descriptions_are_the_default():
    """
    Failing towards 'description' is the safe direction: a description
    wrongly classified as a record silently leaves freshness unchecked,
    while a record wrongly classified as a description merely shows up in a
    coverage gap where somebody will look at it.
    """
    for node_id in ("tool-analyze-screen", "system-tala-agent-architecture",
                    "program-doc-python-harness", "widget_api_bridge_v1"):
        assert purpose_of(node_id) == PURPOSE_DESCRIPTION, node_id


def test_a_declaration_beats_the_guess():
    """
    The prefix rule is a default for documents written before there was
    anything to declare. Anything that declares its purpose is taken at its
    word — otherwise the metadata the reviewer asked for would be decoration.
    """
    assert purpose_of("benchmark-run-1", PURPOSE_DESCRIPTION) == PURPOSE_DESCRIPTION
    assert purpose_of("tool-whatever", PURPOSE_RECORD) == PURPOSE_RECORD


def test_an_unknown_declaration_falls_back_rather_than_propagating():
    assert purpose_of("tool-whatever", "nonsense") == PURPOSE_DESCRIPTION
    assert purpose_of("benchmark-run-1", "nonsense") == PURPOSE_RECORD


def test_the_purposes_are_the_three_the_review_named():
    assert set(DOCUMENT_PURPOSES) == {"record", "description", "mixed"}


# ---------------------------------------------------------------------------
# Records are out of scope, not unchecked
# ---------------------------------------------------------------------------


def test_records_are_excluded_from_freshness_entirely():
    report = documentation_staleness()
    record_ids = {r["node_id"] for r in report["records"]}
    assert record_ids, "no records were recognised; the classification is not running"

    unknown_ids = {u["node_id"] for u in report["unknown"]}
    stale_ids = {s["node_id"] for s in report["stale"]}
    assert not (record_ids & unknown_ids), "a record was counted as a coverage gap"
    assert not (record_ids & stale_ids), (
        "a record was reported stale — regenerating it would destroy the evidence"
    )


def test_a_benchmark_is_never_regenerated_against_current_code():
    """
    The concrete case. These are execution evidence: what the system did on a
    particular day against a particular commit.
    """
    report = documentation_staleness()
    benchmarks = [r for r in report["records"] if r["node_id"].startswith("benchmark-")]
    assert benchmarks, "benchmark runs are no longer classified as records"


def test_the_report_says_what_the_number_means():
    report = documentation_staleness()
    assert "records" in report["note"] and "coverage gap" in report["note"]
    assert "eliminating" in report["note"], (
        "the report should say that driving records to zero is not the goal"
    )


# ---------------------------------------------------------------------------
# Coverage of current descriptions — the actual milestone
# ---------------------------------------------------------------------------


def test_coverage_arithmetic_adds_up():
    report = documentation_staleness()
    coverage = report["coverage"]
    assert coverage["with_source"] + coverage["without_source"] == coverage["descriptions"]
    assert coverage["descriptions"] + coverage["records_excluded"] == report["checked"]


def test_most_current_descriptions_are_checkable():
    """
    A ratchet, not a target. It stood at 123 of 254 when purpose was
    introduced; tool and program documentation closed most of the gap.
    """
    coverage = documentation_staleness()["coverage"]
    assert coverage["with_source"] >= 235, (
        f"description coverage fell to {coverage['with_source']}/"
        f"{coverage['descriptions']}"
    )


def test_tool_documentation_names_its_source():
    """
    The largest block of operational documentation in the store, and it
    carried an empty source-files attribute — 99 descriptions of what tools do
    that nothing could check against the tools. The registry knows the
    function and the function knows its file.
    """
    nodes = resolve_active_nodes_dir()
    tool_nodes = sorted(nodes.glob("tool-*.html"))
    assert len(tool_nodes) > 80, f"only {len(tool_nodes)} tool nodes"

    without = [
        p.stem for p in tool_nodes
        if not (re.search(r'data-source-files="([^"]+)"', p.read_text(encoding="utf-8", errors="replace")))
    ]
    # MCP-bridged tools have no local source, and claiming one would make a
    # description look checkable when nothing can check it.
    assert len(without) <= 5, f"tool nodes with no source reference: {without[:10]}"


def test_the_contract_is_itself_checkable():
    """
    A document that tells everything else to be checkable and is not itself
    is the weakest link in the chain it sits at the top of.
    """
    node = resolve_active_nodes_dir() / "system-architecture-contract.html"
    html = node.read_text(encoding="utf-8", errors="replace")
    files = re.search(r'data-source-files="([^"]+)"', html)
    assert files, "the architecture contract records no source references"
    named = files.group(1)
    for expected in ("architecture_facts.py", "node_execution.py", "preload.js"):
        assert expected in named, f"the contract does not track {expected}"


def test_the_remaining_gap_is_named_not_hidden():
    """
    What is left needs a person to choose its references: concept documents
    that describe an idea rather than a file. Naming them is the point —
    an unexplained residue is how a coverage number stops being acted on.
    """
    report = documentation_staleness()
    remaining = {u["node_id"] for u in report["unknown"]}
    assert len(remaining) <= 20, f"the gap grew to {len(remaining)}: {sorted(remaining)}"
    # These are concept/spec documents, not derived from one source file.
    assert any(n.startswith("system-") for n in remaining), (
        "the residue is no longer the hand-authored concept documents; "
        f"re-examine what is left: {sorted(remaining)}"
    )


def test_the_daily_pass_does_not_strip_the_contract_of_its_references():
    """
    Caught by test ordering, and a real fault rather than an artifact of it.

    The maintenance pass built its own `create_documentation_node` call
    instead of reusing the generator's arguments, so a daily refresh rewrote
    the contract WITHOUT the source files the command line gives it — the
    document that tells everything else to be checkable, made uncheckable by
    the thing whose job is maintaining it. Two argument lists for one node is
    the shape of the bug; one shared constant is the fix.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from generate_architecture_contract import CONTRACT_SOURCES

    maintenance = (ROOT / "python" / "harness" / "doc_maintenance.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "CONTRACT_SOURCES" in maintenance, (
        "the daily pass names its own source list; it will drift from the "
        "generator's and strip references the command line adds"
    )
    assert "architecture_facts.py" in CONTRACT_SOURCES[0] or any(
        "architecture_facts.py" in s for s in CONTRACT_SOURCES
    )

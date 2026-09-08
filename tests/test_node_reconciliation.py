"""
The older architecture documents must say what supersedes them.

Steven's instruction was explicit about the shape of this work:

    "I'd prioritize a small, authoritative set of architecture and operational
    nodes, then reconcile older documents against them. Regenerating everything
    without resolving contradictions would preserve the confusion."

Reading them, those documents are mostly accurate. What they lacked was
standing: nothing said when they were last true, or which document wins where
two disagree. A model reading one has no way to tell whether it is looking at
the current system or at a description from June — and it will not go and
check, because nothing tells it there is anything to check against.

So each carries a reconciliation block naming the contract as authoritative on
five specific subjects, and the date and commit at which it was reconciled.
The date is the load-bearing part: an undated document cannot be reconciled
against anything, because a reader cannot tell which one came first.

These tests keep that true. Adding an architecture node without reconciling it
fails here rather than quietly joining the pile.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.reconcile_architecture_nodes import (
    AUTHORITATIVE_ON,
    AUTHORITY_NODE,
    RECONCILED_NODES,
    SECTION_ID,
)

NODES = Path(__file__).resolve().parents[1] / "nodes"

RERUN = "Run: python scripts/reconcile_architecture_nodes.py"


def _node(node_id: str) -> str:
    path = NODES / f"{node_id}.html"
    if not path.exists():
        pytest.skip(f"{node_id} is not in this install's node store")
    return path.read_text(encoding="utf-8", errors="replace")


def _state(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="node-state"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    assert match, "the node has no state block"
    return json.loads(match.group(1))


@pytest.mark.parametrize("node_id", RECONCILED_NODES)
def test_every_architecture_node_names_its_authority(node_id):
    html = _node(node_id)
    assert f'<section id="{SECTION_ID}">' in html, (
        f"{node_id} does not say what supersedes it. {RERUN}"
    )
    assert AUTHORITY_NODE in html


@pytest.mark.parametrize("node_id", RECONCILED_NODES)
def test_the_reconciliation_is_readable_by_code_too(node_id):
    """
    The prose block is for a model reading the document. Anything that parses
    nodes should be able to find the same fact without reading English.
    """
    state = _state(_node(node_id))
    record = state.get("contract_reconciliation")
    assert record, f"{node_id} carries no machine-readable reconciliation. {RERUN}"
    assert record["authority"] == AUTHORITY_NODE
    assert record["authoritative_on"] == list(AUTHORITATIVE_ON)


@pytest.mark.parametrize("node_id", RECONCILED_NODES)
def test_each_says_when_it_was_reconciled(node_id):
    """
    Undated is the state that made all of this necessary. A document with no
    date cannot be compared against a newer one, so nobody compares it.
    """
    record = _state(_node(node_id))["contract_reconciliation"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["reconciled_at"]), record
    assert re.fullmatch(r"[0-9a-f]{40}", record["reconciled_commit"]), record


@pytest.mark.parametrize("node_id", RECONCILED_NODES)
def test_stamping_did_not_damage_the_node(node_id):
    """
    These are hand-edited files with a parser contract. A reconciliation that
    broke the node it annotated would be a poor trade.
    """
    html = _node(node_id)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<article" in html and "</article>" in html
    assert 'data-schema-version="neural-node/v2"' in html
    _state(html)  # raises if the JSON no longer parses
    assert html.count(f'<section id="{SECTION_ID}">') == 1, (
        "the reconciliation block was stacked rather than replaced"
    )


def test_the_deference_is_bounded():
    """
    The contract wins on the subjects it covers and nowhere else. A blanket
    'the contract is authoritative' would discard what these documents know
    and it does not — which is a different way of losing the same knowledge.
    """
    html = _node(RECONCILED_NODES[0])
    assert "On everything else this node remains the better source" in html
    assert len(AUTHORITATIVE_ON) >= 3, "the bounded list has been emptied"


def test_the_authority_node_is_not_reconciled_against_itself():
    assert AUTHORITY_NODE not in RECONCILED_NODES


def test_the_checker_can_actually_fail(tmp_path):
    """
    A reconciliation checker that always passes checks nothing. This pins the
    detection, not the result.
    """
    from scripts.reconcile_architecture_nodes import _SECTION_RE

    assert _SECTION_RE.search(_node(RECONCILED_NODES[0]))
    assert not _SECTION_RE.search("<html><body>nothing here</body></html>")

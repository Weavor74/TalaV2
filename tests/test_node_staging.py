"""
Probation for newly authored nodes.

A node Tala writes lands in nodes/_staging/ and only joins the retrieval
corpus after passing verification on several consecutive cycles. These
tests pin the properties that make that worth having:

- a junk node never reaches production, however many cycles run;
- a good node reaches it only after the required number of cycles, not on
  the first one;
- staged nodes are invisible to everything that reads the wiki, which is
  what makes the whole design safe to bolt on.

TALA_USER_DATA is pinned in every test — resolve_active_nodes_dir() is the
single canonical path, and without the pin these would read or write the
real install's nodes directory.
"""

from __future__ import annotations

import pytest

from sdk.agent import node_staging


VALID_NODE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="tala-schema" content="neural-node/v2"><title>T</title></head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00">
  <header id="node-identity"><h1 id="node-title">Test Node</h1></header>
  <section id="semantic-memory"><p>{body}</p></section>
</article>
</body>
</html>
"""

REAL_BODY = (
    "This node records a genuine, specific piece of knowledge with enough "
    "substance to be worth retrieving later, well past the minimum length."
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.delenv("TALA_NODE_STAGING", raising=False)
    monkeypatch.setenv("TALA_NODE_PROMOTION_PASSES", "3")
    (tmp_path / "nodes").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _nodes(tmp_path):
    return tmp_path / "nodes"


def test_staged_node_is_invisible_to_the_wiki(_isolate):
    """
    The safety property the whole design rests on: nodes/_staging is a
    SUBdirectory, and every consumer globs nodes/*.html non-recursively, so
    an unverified node cannot reach retrieval, the node grid, or context
    injection while it waits.
    """
    node_staging.stage_node("staged-thing", VALID_NODE.format(node_id="staged-thing", body=REAL_BODY))

    production = sorted(p.name for p in _nodes(_isolate).glob("*.html"))
    assert production == [], "a staged node must not appear in the production nodes directory"
    assert (node_staging.resolve_staging_dir() / "staged-thing.html").is_file()


def test_promotion_requires_the_full_number_of_cycles(_isolate):
    node_staging.stage_node("good-node", VALID_NODE.format(node_id="good-node", body=REAL_BODY))

    for cycle in (1, 2):
        summary = node_staging.run_verification_cycle()
        assert summary["promoted"] == [], f"promoted after only {cycle} cycle(s)"
        assert not (_nodes(_isolate) / "good-node.html").exists()

    summary = node_staging.run_verification_cycle()
    assert summary["promoted"] == ["good-node"]
    assert (_nodes(_isolate) / "good-node.html").is_file()
    assert not (node_staging.resolve_staging_dir() / "good-node.html").exists()


@pytest.mark.parametrize(
    "body, node_id, why",
    [
        (REAL_BODY, "wrong-name", "node_id does not match filename"),
        ("too short", "thin-node", "semantic memory too thin"),
        (
            "Auto-scaffolded Neural Node for thing, created by Tala's self-healing "
            "documentation system because no Neural Node existed yet for the module.",
            "stub-node",
            "placeholder text",
        ),
    ],
    ids=["id-mismatch", "too-thin", "placeholder"],
)
def test_junk_never_reaches_production(_isolate, body, node_id, why):
    """Whatever the defect, more cycles must not turn it into a wiki node."""
    filename_id = "thin-node" if node_id == "thin-node" else ("stub-node" if node_id == "stub-node" else "right-name")
    html = VALID_NODE.format(node_id=node_id, body=body)
    node_staging.stage_node(filename_id, html)

    for _ in range(6):
        summary = node_staging.run_verification_cycle()
        assert summary["promoted"] == [], f"junk promoted despite {why}"

    assert not (_nodes(_isolate) / f"{filename_id}.html").exists()
    quarantined = list(node_staging.resolve_quarantine_dir().glob("*.html"))
    assert quarantined, "a repeatedly failing node must end up quarantined, not retried forever"


def test_embedded_python_that_does_not_compile_is_rejected(_isolate):
    """A skill node whose code cannot compile can never be executed."""
    broken = VALID_NODE.format(node_id="broken-skill", body=REAL_BODY).replace(
        "</article>",
        '<script type="application/x-python" id="node-executable">def f(:\n</script></article>',
    )
    path = node_staging.stage_node("broken-skill", broken)

    ok, reason = node_staging.verify_node_file(path)
    assert ok is False
    assert "compile" in (reason or "")


def test_learning_nodes_are_staged_not_written_live(_isolate):
    """
    The path that produced the junk: create_learning_documentation_node is
    the model deciding something is worth remembering, so it must land on
    probation rather than straight in the corpus.
    """
    from sdk.agent.documentation_nodes import create_learning_documentation_node

    result = create_learning_documentation_node(
        topic="A specific and genuinely useful lesson about retry backoff",
        summary=(
            "Exponential backoff with a cap prevented the poll loop from hammering "
            "a failing endpoint, which is the behaviour we want to keep."
        ),
        details=["Cap the delay", "Reset on success"],
        tags=["test"],
    )

    payload = result.as_dict() if hasattr(result, "as_dict") else result
    assert payload.get("staged") is True
    assert sorted(p.name for p in _nodes(_isolate).glob("*.html")) == []


def test_staging_can_be_turned_off(_isolate, monkeypatch):
    """An operator must be able to opt out without editing code."""
    monkeypatch.setenv("TALA_NODE_STAGING", "off")
    assert node_staging.staging_enabled() is False

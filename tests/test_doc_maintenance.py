"""
The daily pass that keeps Tala's self-description current.

Steven asked for regeneration at least every day, after we found that the
self-healing he remembered does not run: no git hooks, no scheduled job, and
regeneration only when somebody asks in chat, calls the RPC, or completes a
goal. The contract had been wrong about what Tala could execute for weeks.

Two properties matter, and they pull against each other.

It must **write when the system changed**, or it is decoration.

It must **not write when it did not**, or a year of daily rewrites — each one
a file diff and a transaction anchored on the chain — buries the days that
meant something. A log full of nothing teaches you to stop reading it.

So the comparison deliberately ignores the verification stamp: the timestamp
and commit move on their own, and treating that as a change would rewrite the
node every single day while the description stayed identical.
"""

from __future__ import annotations

import asyncio

import pytest

from python.harness import doc_maintenance


@pytest.fixture
def contract_node(tmp_path, monkeypatch):
    """Point node writes at a scratch directory, not the real store."""
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    from sdk.agent import documentation_nodes

    monkeypatch.setattr(documentation_nodes, "resolve_active_nodes_dir", lambda: nodes)
    monkeypatch.setattr(doc_maintenance, "resolve_active_nodes_dir", lambda: nodes,
                        raising=False)
    return nodes / f"{doc_maintenance.CONTRACT_NODE_ID}.html"


# ---------------------------------------------------------------------------
# The comparison itself, which is where the judgement lives
# ---------------------------------------------------------------------------


def test_the_stamp_alone_is_not_a_change():
    """
    Every generation carries a fresh timestamp and the current commit. If
    those counted, the node would be rewritten daily forever while saying the
    same thing.
    """
    monday = {"capabilities": {"executable_node_types": ["workflow"]},
              "verification": {"generated_at": "2026-09-07T00:00:00Z",
                               "reviewed_commit": "aaa"}}
    tuesday = {"capabilities": {"executable_node_types": ["workflow"]},
               "verification": {"generated_at": "2026-09-08T00:00:00Z",
                                "reviewed_commit": "bbb"}}
    assert doc_maintenance._substance(monday) == doc_maintenance._substance(tuesday)


def test_a_real_difference_is_a_change():
    before = {"capabilities": {"executable_node_types": ["workflow"]},
              "verification": {"generated_at": "2026-09-07T00:00:00Z"}}
    after = {"capabilities": {"executable_node_types": ["workflow", "function"]},
             "verification": {"generated_at": "2026-09-07T00:00:00Z"}}
    assert doc_maintenance._substance(before) != doc_maintenance._substance(after)


def test_key_order_is_not_a_change():
    """Serialisation order must not masquerade as the system moving."""
    one = {"a": 1, "b": {"x": 1, "y": 2}}
    two = {"b": {"y": 2, "x": 1}, "a": 1}
    assert doc_maintenance._substance(one) == doc_maintenance._substance(two)


def test_nothing_published_yet_counts_as_a_change():
    assert doc_maintenance._substance(None) == ""


# ---------------------------------------------------------------------------
# End to end, against a real generated contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_settles_and_then_stops_writing(contract_node):
    """
    Convergence, which is the property that matters — not stillness on the
    second pass exactly.

    The contract publishes a node count, and publishing the contract IS a
    node, so in an empty store the first write moves the number it just
    reported. That settles on the following pass and must then stay settled.
    A pass that kept finding a difference would rewrite the file and anchor a
    chain transaction every single day, which is the churn this comparison
    exists to prevent.
    """
    first = await doc_maintenance.regenerate_contract_if_changed()
    assert first["ran"] is True, first.get("reason")
    assert first["wrote"] is True, "nothing was published on a first run"
    assert contract_node.exists()

    # Allow it to settle: at most one further write, for the count it changed
    # by existing.
    settling = await doc_maintenance.regenerate_contract_if_changed()
    assert settling["ran"] is True

    stamp = contract_node.stat().st_mtime
    for _ in range(3):
        again = await doc_maintenance.regenerate_contract_if_changed()
        assert again["changed"] is False, "an unchanged system reported as changed"
        assert again["wrote"] is False, "the node was rewritten with identical content"
    assert contract_node.stat().st_mtime == stamp


@pytest.mark.asyncio
async def test_a_change_in_the_code_is_picked_up(contract_node, monkeypatch):
    """
    The property the whole pass exists for: the code moves, and the
    description follows without anybody asking it to.
    """
    await doc_maintenance.regenerate_contract_if_changed()
    before = contract_node.read_text(encoding="utf-8", errors="replace")

    from sdk.agent import node_execution

    monkeypatch.setattr(
        node_execution, "EXECUTABLE_TYPES",
        frozenset(set(node_execution.EXECUTABLE_TYPES) | {"pipeline"}),
    )

    result = await doc_maintenance.regenerate_contract_if_changed()
    assert result["changed"] is True and result["wrote"] is True, (
        "the code gained an executable node type and the contract did not follow"
    )
    after = contract_node.read_text(encoding="utf-8", errors="replace")
    assert "pipeline" in after and "pipeline" not in before


# ---------------------------------------------------------------------------
# It must never be able to take the harness down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failure_is_reported_rather_than_raised(monkeypatch):
    async def exploding():
        raise RuntimeError("no database")

    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import generate_architecture_contract as gen

    monkeypatch.setattr(gen, "build_contract", exploding)
    result = await doc_maintenance.regenerate_contract_if_changed()
    assert result["ran"] is False
    assert "no database" in result["reason"]


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_pass(monkeypatch):
    """
    A maintenance loop that dies on one bad day stops maintaining anything,
    silently, which is how the last one ended up not running.
    """
    calls = []

    async def failing():
        calls.append(1)
        raise RuntimeError("transient")

    monkeypatch.setattr(doc_maintenance, "regenerate_contract_if_changed", failing)
    monkeypatch.setattr(doc_maintenance, "FIRST_RUN_DELAY_SECONDS", 0)
    monkeypatch.setattr(doc_maintenance, "INTERVAL_SECONDS", 0.01)

    task = asyncio.create_task(doc_maintenance.run_forever())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) > 1, "the loop stopped after the first failure"


def test_it_runs_daily_at_most():
    """The interval is the promise. A tighter one would be churn."""
    assert doc_maintenance.INTERVAL_SECONDS == 24 * 60 * 60
    assert doc_maintenance.FIRST_RUN_DELAY_SECONDS > 0, (
        "running at startup competes with database and provider initialisation"
    )


@pytest.mark.asyncio
async def test_it_can_be_asked_for_rather_than_waited_on():
    """
    Daily is a floor. After changing something structural you should be able
    to make the self-description catch up now, instead of reasoning against a
    stale contract until tomorrow.
    """
    import json as _json

    from python.harness import server

    assert "docs.regenerate_contract" in server._HANDLERS

    raw = await server._HANDLERS["docs.regenerate_contract"]({}, 1)
    payload = _json.loads(raw)
    assert "error" not in payload, payload
    assert payload["result"]["ran"] is True

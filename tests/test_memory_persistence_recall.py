"""
test_memory_persistence_recall.py

End-to-end coverage for memory store -> recall against the real Postgres
dev database (matches the pattern already used by tests/test_fleet_engineering.py:
init_pool()/close_pool() around a real DB, not mocks). This is deliberately
NOT mocked: every prior memory test (tests/test_memory.py) patches
db.fetch/db.execute, which only proves the SQL text looks right, not that a
fact recorded in one call is actually recoverable through the real
full-text-search + trigger + dedup pipeline in a later, independent call --
the exact property the "self-improvement/swarm/memory have never been put
through the same scrutiny as tool-calling" gap calls out.

Every test uses a unique random marker token as the searched-for content so
it can't collide with real data already sitting in the dev DB, and cleans
up its own rows afterward.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from python.harness import db
from python.harness.memory import (
    handle_memory_browse,
    handle_memory_record_completed_turn,
    handle_memory_search,
)
from sdk.agent.memory.manager import get_manager
from sdk.agent.memory.local_provider import LocalMemoryProvider


@pytest.fixture(autouse=True)
def setup_memory_providers():
    manager = get_manager()
    if not manager._providers:
        manager.register_provider(LocalMemoryProvider())


def _marker() -> str:
    return "talatestmarker" + uuid.uuid4().hex


async def _cleanup_session(session_id: str) -> None:
    await db.execute("DELETE FROM conversation_turns WHERE session_id = $1", session_id)
    await db.execute("DELETE FROM memory_index WHERE source_type = 'session' AND ref_id = $1", session_id)
    await db.execute("DELETE FROM memory_recall_events WHERE recalled_id LIKE 'mem-%'")


@pytest.mark.asyncio
async def test_store_then_recall_in_a_later_independent_search_call():
    """Record a fact in one call, then recall it via a completely separate
    handle_memory_search call -- the "store a fact, recall it later" scenario."""
    await db.init_pool()
    session_id = f"test-mem-{uuid.uuid4().hex[:8]}"
    other_session_id = f"test-mem-other-{uuid.uuid4().hex[:8]}"
    marker = _marker()
    try:
        await handle_memory_record_completed_turn({
            "session_id": session_id,
            "role": "user",
            "content": f"The secret ingredient is {marker}.",
        })
        # Unrelated activity in between, to prove recall isn't just an echo
        # of whatever was inserted most recently.
        await handle_memory_record_completed_turn({
            "session_id": other_session_id,
            "role": "user",
            "content": "This turn is unrelated noise and should not match.",
        })

        result = await handle_memory_search({"query": marker, "sources": ["session"]})

        assert result["results"], f"expected the stored marker {marker!r} to be recallable"
        hit = result["results"][0]
        assert hit["source_type"] == "session"
        assert hit["ref"]["id"] == session_id
        assert marker in hit["snippet"]
    finally:
        await _cleanup_session(session_id)
        await _cleanup_session(other_session_id)
        await db.close_pool()


@pytest.mark.asyncio
async def test_duplicate_turn_content_is_not_double_indexed():
    """LocalMemoryProvider.store() has a WHERE NOT EXISTS dedup guard;
    verify it actually prevents a duplicate row in the real table rather
    than just asserting the SQL string contains the clause."""
    await db.init_pool()
    session_id = f"test-mem-dedup-{uuid.uuid4().hex[:8]}"
    marker = _marker()
    content = f"Duplicate fact: {marker}"
    try:
        await handle_memory_record_completed_turn({
            "session_id": session_id, "role": "user", "content": content,
        })
        await handle_memory_record_completed_turn({
            "session_id": session_id, "role": "user", "content": content,
        })

        count = await db.fetchval(
            "SELECT COUNT(*) FROM memory_index WHERE source_type = 'session' AND ref_id = $1 AND content_text = $2",
            session_id, content,
        )
        assert count == 1
    finally:
        await _cleanup_session(session_id)
        await db.close_pool()


@pytest.mark.asyncio
async def test_recall_event_is_logged_against_the_real_result_id():
    await db.init_pool()
    session_id = f"test-mem-recall-{uuid.uuid4().hex[:8]}"
    marker = _marker()
    try:
        await handle_memory_record_completed_turn({
            "session_id": session_id, "role": "user", "content": f"Recall-logging fact {marker}",
        })

        result = await handle_memory_search({"query": marker, "sources": ["session"]})
        assert result["results"]
        recalled_id = result["results"][0]["id"]

        logged = await db.fetchval(
            "SELECT COUNT(*) FROM memory_recall_events WHERE query = $1 AND recalled_id = $2",
            marker, recalled_id,
        )
        assert logged == 1
    finally:
        await _cleanup_session(session_id)
        await db.execute("DELETE FROM memory_recall_events WHERE query = $1", marker)
        await db.close_pool()


@pytest.mark.asyncio
async def test_private_profile_field_is_redacted_unless_allow_private():
    """handle_memory_browse strips pii_vault from profile context by
    default -- verified against a real profiles row, matching the class of
    privacy-leak bug already found/fixed elsewhere this session (ghost
    chain push-side leak)."""
    await db.init_pool()
    profile_id = f"test-profile-{uuid.uuid4().hex[:8]}"
    secret_marker = _marker()
    try:
        await db.execute(
            """
            INSERT INTO profiles (id, type, name, config)
            VALUES ($1, 'human', 'Test Profile', $2::jsonb)
            """,
            profile_id,
            f'{{"pii_vault": {{"note": "{secret_marker}"}}, "identity": {{"name": "Test"}}}}',
        )

        redacted = await handle_memory_browse({
            "source_type": "profile", "ref_id": profile_id, "allow_private": False,
        })
        assert secret_marker not in redacted["context"]

        full = await handle_memory_browse({
            "source_type": "profile", "ref_id": profile_id, "allow_private": True,
        })
        assert secret_marker in full["context"]
    finally:
        await db.execute("DELETE FROM profiles WHERE id = $1", profile_id)
        await db.close_pool()


@pytest.mark.asyncio
async def test_learning_cue_produces_a_node_recallable_by_later_search(tmp_path, monkeypatch):
    """The auto-learning path: an assistant turn containing a learning cue
    ("remember that ...") should produce a documentation node that is
    itself indexed and recallable by a later, independent search -- the
    full "durable learning" loop the capability assessment claims exists."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    await db.init_pool()
    session_id = f"test-mem-learn-{uuid.uuid4().hex[:8]}"
    marker = _marker()
    node_id = None
    try:
        result = await handle_memory_record_completed_turn({
            "session_id": session_id,
            "role": "assistant",
            "content": f"Remember that the deployment marker is {marker}.",
        })

        assert result.get("learning_node_path"), "expected a learning node to be created"
        node_path = result["learning_node_path"]
        node_id = result.get("learning_node_id")
        assert (tmp_path / "nodes" / f"{node_id}.html").exists() or Path(node_path).exists()

        # Deliberately omit "sources" -- this must be recallable through the
        # *default* search sources, since nothing about the auto-learning
        # path is discoverable by a caller who doesn't already know it's
        # indexed under source_type "documentation". Require the hit to
        # specifically be the documentation node (not just the raw
        # conversation turn, which also contains the marker and is indexed
        # under source_type "session" regardless of this fix).
        search_result = await handle_memory_search({"query": marker})
        doc_hits = [r for r in search_result["results"] if r["source_type"] == "documentation"]
        assert doc_hits, (
            "expected a documentation-sourced hit for the learning node in default search, got "
            f"source_types={[r['source_type'] for r in search_result['results']]}"
        )
        assert doc_hits[0]["ref"]["id"] == node_id
    finally:
        await _cleanup_session(session_id)
        if node_id:
            await db.execute("DELETE FROM memory_index WHERE ref_id = $1", node_id)
        await db.close_pool()

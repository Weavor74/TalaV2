"""
The table the VPS write gate reads, which no migration ever created.

Installs that have an `embeddings` table got it from a migration since
removed; it survived by inheritance. Its absence only appears on a genuinely
fresh database -- which is what rebuilding talacore.app produced:

    Failed to seed approved VPS memory context:
    relation "embeddings" does not exist

Not cosmetic. VPSWriteStateMachine.validate decides whether a write to a
connection node is permitted by finding an approved context in this table.
No table means no approved context means every such write is DENIED, so a
brand-new install could never connect to the VPS -- and the reason surfaced
as "Memory context validation failed", which names neither the table nor the
missing extension.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from python.harness import db

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool():
    await db.init_pool()
    yield
    await db.close_pool()


async def _pgvector_available() -> bool:
    row = await db.fetchrow("SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'")
    return row is not None


async def _require_pgvector():
    """
    Skip rather than fail where pgvector was never provisioned.

    Creating the extension needs superuser, which the app's role is not, so
    this is an environment fact and not a defect in the code under test. The
    skip names the fix, because a silent skip here hides the thing that makes
    a fresh install unable to connect to a connection node.
    """
    if not await _pgvector_available():
        pytest.skip(
            "pgvector is not enabled in this database. Run scripts/bootstrap-db.sh "
            "as an administrator; until then the embeddings table cannot exist and "
            "writes to a connection node are denied."
        )


async def test_the_table_exists_after_migrations(pool):
    await _require_pgvector()
    row = await db.fetchrow("SELECT to_regclass('public.embeddings') AS t")
    assert row["t"] is not None, "the VPS write gate has no table to read"


async def test_it_has_the_columns_the_gate_and_the_seed_use(pool):
    await _require_pgvector()
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'embeddings'"
    )
    columns = {r["column_name"] for r in rows}

    # seed_approved_vps_context inserts exactly these.
    for needed in (
        "id", "owner_kind", "owner_id", "chunk_index", "embedding_model",
        "content", "content_hash", "embedding", "metadata",
    ):
        assert needed in columns, f"seed_approved_vps_context writes {needed}, which does not exist"


async def test_the_harness_reports_a_missing_extension_rather_than_hiding_it(pool, caplog):
    """
    The failure that mattered was silent. Whatever the environment, running
    the schema step must either succeed or say plainly what is wrong and what
    it costs -- "Memory context validation failed" named neither.
    """
    with caplog.at_level("WARNING"):
        await db._ensure_embeddings_schema()

    if await _pgvector_available():
        row = await db.fetchrow("SELECT to_regclass('public.embeddings') AS t")
        assert row["t"] is not None
    else:
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "pgvector" in messages or "vector" in messages
        assert "denied" in messages.lower() or "DENIED" in messages


async def test_the_approved_context_is_seeded(pool):
    await _require_pgvector()
    """Without a row here every VPS write is denied, whatever else is right."""
    row = await db.fetchrow(
        "SELECT count(*) AS n FROM embeddings WHERE owner_kind = 'system-approved'"
    )
    assert row["n"] >= 1, "no approved context: connecting to a node would be denied"


async def test_creating_the_schema_is_idempotent(pool):
    await _require_pgvector()
    """It runs on every startup."""
    await db._ensure_embeddings_schema()
    await db._ensure_embeddings_schema()

    row = await db.fetchrow("SELECT to_regclass('public.embeddings') AS t")
    assert row["t"] is not None

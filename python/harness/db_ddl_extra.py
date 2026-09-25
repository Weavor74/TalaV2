"""
db_ddl_extra.py -- migration statements appended to db.py's _DDL_STATEMENTS.

db.py is pinned at its historical size ratchet budget (tests/test_size_ratchet.py):
"move code out into a module instead of raising the budget" is that test's own
stated remedy, and db.py's list-of-DDL-strings shape means a migration can live
here and still run in the same init_pool() pass -- no special-casing needed.
"""
from __future__ import annotations

_INFERENCE_PROVIDER_EXTRA_DDL: list[str] = [
    # The embedded-builtin INSERT's ON CONFLICT DO NOTHING only sets the
    # model on a fresh install; this self-heals a row stuck on the
    # pre-1.5B-upgrade value without touching any other install's choice.
    #
    # inference.save_providers double-json-encodes config on write (the same
    # class of bug as tasks.payload: a jsonb codec plus a json.dumps() caller
    # means the column ends up holding a JSON *string*, not an object), so a
    # provider ever saved through that path has jsonb_typeof(config) =
    # 'string' rather than 'object'. Both shapes are matched here; only the
    # write path's own bug is out of scope for this fix.
    """
    UPDATE inference_providers
    SET config = '{"model": "Qwen/Qwen2.5-1.5B-Instruct-GGUF"}'::jsonb
    WHERE id = 'embedded-builtin' AND (
        config->>'model' = 'qwen2.5-0.5b'
        OR (jsonb_typeof(config) = 'string'
            AND (config #>> '{}')::jsonb->>'model' = 'qwen2.5-0.5b')
    );
    """,
    # inference.save_providers replaces the whole table on every save, so a
    # removed provider whose model is still installed looked identical to
    # one never seen -- auto-detect always re-added it. This tombstone is
    # what makes a removal stick.
    """
    CREATE TABLE IF NOT EXISTS dismissed_inference_providers (
        id           TEXT        PRIMARY KEY,
        dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
]

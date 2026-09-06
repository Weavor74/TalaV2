"""
db.py — Tala Cognitive Engine Room: PostgreSQL Database Layer
=============================================================
The SINGLE authoritative durable-state layer for the entire Tala harness.

Invariant
---------
ALL reads and writes of persistent state MUST flow through this module.
mem0 is NEVER used as a durable authority — it is a read-only semantic
cache whose writes are blocked by :mod:`mem0_adapter`.

Exposed API
-----------
- ``init_pool()``   — create the asyncpg connection pool and run migrations
- ``close_pool()``  — drain and close the pool on shutdown
- ``get_pool()``    — return the live pool (raises if not initialized)
- ``execute()``     — run a DML statement and return status string
- ``fetch()``       — run a query and return list[Record]
- ``fetchrow()``    — run a query and return a single Record | None
- ``assert_postgres_authority()`` — invariant guard used by other modules

Milestone 1.2 — Task 1.2.2
"""

from __future__ import annotations

import logging
import os
import textwrap
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
from asyncpg import Pool, Record

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_pool: Pool | None = None

# Token that mem0_adapter embeds in the call-stack locals so we can detect
# illegal write attempts originating from that context.
_MEM0_CONTEXT_TOKEN = "__tala_mem0_ctx__"


# ---------------------------------------------------------------------------
# DDL — table definitions
# ---------------------------------------------------------------------------

_DDL_STATEMENTS: list[str] = [
    # ------------------------------------------------------------------
    # nodes — core knowledge graph nodes
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS nodes (
            id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            node_type           TEXT        NOT NULL,
            execution_authority TEXT        NOT NULL DEFAULT 'user',
            content_json        JSONB       NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # Index for fast type-based lookups
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_nodes_type
            ON nodes (node_type);
    """),
    # Auto-update updated_at via trigger
    textwrap.dedent("""\
        CREATE OR REPLACE FUNCTION tala_set_updated_at()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$;
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_nodes_updated_at'
            ) THEN
                CREATE TRIGGER trg_nodes_updated_at
                BEFORE UPDATE ON nodes
                FOR EACH ROW EXECUTE FUNCTION tala_set_updated_at();
            END IF;
        END;
        $$;
    """),

    # ------------------------------------------------------------------
    # token_ledger — immutable append-only accounting of every inference
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS token_ledger (
            id                  BIGSERIAL   PRIMARY KEY,
            session_id          TEXT        NOT NULL,
            prompt_tokens       INTEGER     NOT NULL DEFAULT 0,
            completion_tokens   INTEGER     NOT NULL DEFAULT 0,
            cost_usd            NUMERIC(18, 10) NOT NULL DEFAULT 0,
            model               TEXT        NOT NULL DEFAULT '',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_token_ledger_session
            ON token_ledger (session_id);
    """),

    # ------------------------------------------------------------------
    # wallet — single-row token economy balance sheet
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS wallet (
            id                  INTEGER     PRIMARY KEY DEFAULT 1,
            crypto_balance      NUMERIC(28, 8) NOT NULL DEFAULT 0,
            ai_token_balance    NUMERIC(28, 8) NOT NULL DEFAULT 0,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT wallet_single_row CHECK (id = 1)
        );
    """),
    # Seed the wallet row if it doesn't exist yet
    textwrap.dedent(f"""\
        INSERT INTO wallet (id, ai_token_balance)
        VALUES (1, {settings.INITIAL_AI_TOKEN_BALANCE})
        ON CONFLICT (id) DO NOTHING;
    """),

    # ------------------------------------------------------------------
    # agent_log — structured log sink for all harness subsystems
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS agent_log (
            id          BIGSERIAL   PRIMARY KEY,
            level       TEXT        NOT NULL DEFAULT 'INFO',
            message     TEXT        NOT NULL,
            trace       JSONB       NOT NULL DEFAULT '{}',
            tx_id       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_agent_log_level
            ON agent_log (level);
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_agent_log_created_at
            ON agent_log (created_at DESC);
    """),
    # ------------------------------------------------------------------
    # inference_providers — configured LLM backends (Milestone 15)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS inference_providers (
            id         TEXT        PRIMARY KEY,
            type       TEXT        NOT NULL,
            name       TEXT        NOT NULL,
            priority   INTEGER     NOT NULL,
            status     TEXT        NOT NULL DEFAULT 'unknown',
            config     JSONB       NOT NULL DEFAULT '{}',
            locked     BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        INSERT INTO inference_providers (id, type, name, priority, status, config, locked)
        VALUES (
            'embedded-builtin',
            'embedded',
            'Tala Embedded Engine',
            1,
            'unknown',
            '{"model": "Qwen/Qwen2.5-0.5B-Instruct"}'::jsonb,
            TRUE
        )
        ON CONFLICT (id) DO NOTHING;
    """),
    # ------------------------------------------------------------------
    # agents — configured agent and workforce personas (Milestone 16)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS agents (
            id                  TEXT        PRIMARY KEY,
            name                TEXT        NOT NULL,
            type                TEXT        NOT NULL DEFAULT 'standalone',
            role                TEXT        NOT NULL DEFAULT '',
            crew_type           TEXT        NOT NULL DEFAULT '',
            is_active           BOOLEAN     NOT NULL DEFAULT FALSE,
            config              JSONB       NOT NULL DEFAULT '{}',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        INSERT INTO agents (id, name, type, role, is_active, config)
        VALUES (
            'tala-core',
            'Tala Core',
            'standalone',
            'General Assistant',
            TRUE,
            '{"system_prompt": "You are Tala, the core offline cognitive agent. Help the user complete tasks in their workspace."}'::jsonb
        )
        ON CONFLICT (id) DO NOTHING;
    """),
    # ------------------------------------------------------------------
    # crews — the organisation, as Steven defines it
    # ------------------------------------------------------------------
    #
    # > "Crews are considered employees... Treat it like a business or a
    # > military or a guild. It is an organization piece." (Q-28)
    # >
    # > "There could be 200 crew members broken into teams of 5, and each team
    # > will report to their respective department." (Q-29)
    #
    # A crew is an *entity*, which is what was missing. `agents.crew_type`
    # already existed and is a **category** -- two agents both labelled
    # "Business" share a classification, not a crew (UI-050) -- so it stays as
    # the classification it is, and membership becomes a real foreign key.
    #
    # The distinction from a swarm is control flow, not headcount: a crew is a
    # structured hierarchy running a deterministic process defined upfront,
    # where a swarm is a decentralised mesh whose handoffs emerge at runtime.
    # Nothing here routes emergently, and nothing should.
    #
    # parent_crew_id gives the second level: teams sit inside departments. One
    # self-referencing column rather than separate team and department tables,
    # because "teams of 5 reporting to departments" is a tree and a tree with
    # two hardcoded levels stops being one the moment somebody adds a third.
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS crews (
            id             TEXT        PRIMARY KEY,
            name           TEXT        NOT NULL,
            kind           TEXT        NOT NULL DEFAULT 'team',
            parent_crew_id TEXT        REFERENCES crews(id) ON DELETE SET NULL,
            purpose        TEXT        NOT NULL DEFAULT '',
            description    TEXT        NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_crews_parent
            ON crews(parent_crew_id);
    """),

    # The two columns that make hierarchy expressible at all (UI-049). A grep
    # for parent_id, manager_id, reports_to and supervisor across this file
    # previously returned nothing, so "one boss and many subordinates" could
    # not be written down, let alone acted on.
    #
    # ON DELETE SET NULL on reports_to rather than CASCADE: a manager leaving
    # must not delete their reports. Re-pointing them is a decision with an
    # obvious right answer (their manager's manager) and it is made explicitly
    # in crews.py, not silently by a foreign key.
    textwrap.dedent("""        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agents' AND column_name = 'crew_id'
            ) THEN
                ALTER TABLE agents ADD COLUMN crew_id TEXT
                    REFERENCES crews(id) ON DELETE SET NULL;
                ALTER TABLE agents ADD COLUMN reports_to TEXT
                    REFERENCES agents(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_agents_crew
            ON agents(crew_id);
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_agents_reports_to
            ON agents(reports_to);
    """),

    # ------------------------------------------------------------------
    # crew_runs — a record of running a whole crew (team) at once.
    #
    # crews.py routes *individual* tasks into a crew (assign/escalate). This
    # table records the other thing a team is for: running every member on the
    # same goal concurrently and, when the crew has a manager, letting that
    # manager synthesise the members' answers into one. One row per run; the
    # per-member outputs and the final synthesis live in JSONB so the shape can
    # grow without a migration per field.
    # ------------------------------------------------------------------
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS crew_runs (
            id           TEXT        PRIMARY KEY,
            crew_id      TEXT        NOT NULL,
            crew_name    TEXT        NOT NULL DEFAULT '',
            goal         TEXT        NOT NULL DEFAULT '',
            status       TEXT        NOT NULL DEFAULT 'running',
            manager_id   TEXT        NOT NULL DEFAULT '',
            manager_name TEXT        NOT NULL DEFAULT '',
            members      JSONB       NOT NULL DEFAULT '[]',
            final        TEXT        NOT NULL DEFAULT '',
            error        TEXT        NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        );
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_crew_runs_crew
            ON crew_runs(crew_id, created_at DESC);
    """),

    # ------------------------------------------------------------------
    # guardrails_config — safety configuration presets (Milestone 18)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS guardrails_config (
            id          INTEGER     PRIMARY KEY DEFAULT 1,
            category    TEXT        NOT NULL DEFAULT 'none',
            rules       JSONB       NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT guardrails_config_single_row CHECK (id = 1)
        );
    """),
    textwrap.dedent("""\
        INSERT INTO guardrails_config (id, category, rules)
        VALUES (1, 'none', '{}'::jsonb)
        ON CONFLICT (id) DO NOTHING;
    """),
    # ------------------------------------------------------------------
    # client_settings — local client runtime choices and pairing state
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS client_settings (
            key        TEXT        PRIMARY KEY,
            value      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # search_providers — configured search backends
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS search_providers (
            id            TEXT        PRIMARY KEY,
            name          TEXT        NOT NULL,
            endpoint      TEXT        NOT NULL,
            api_key       TEXT        NOT NULL DEFAULT '',
            priority      INTEGER     NOT NULL,
            locked        BOOLEAN     NOT NULL DEFAULT FALSE,
            credential_id TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # git_providers — configured git hosting backends
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS git_providers (
            id            TEXT        PRIMARY KEY,
            name          TEXT        NOT NULL,
            endpoint      TEXT        NOT NULL,
            username      TEXT        NOT NULL DEFAULT '',
            api_key       TEXT        NOT NULL DEFAULT '',
            priority      INTEGER     NOT NULL,
            locked        BOOLEAN     NOT NULL DEFAULT FALSE,
            credential_id TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # storage_providers — remote / cloud storage backends
    # credentials is a JSONB blob; schema varies per provider type
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS storage_providers (
            id            TEXT        PRIMARY KEY,
            name          TEXT        NOT NULL,
            provider_type TEXT        NOT NULL,
            credentials   JSONB       NOT NULL DEFAULT '{}'::jsonb,
            priority      INTEGER     NOT NULL,
            locked        BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # database_providers — external/replica database servers
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS database_providers (
            id          TEXT        PRIMARY KEY,
            name        TEXT        NOT NULL,
            endpoint    TEXT        NOT NULL,
            username    TEXT        NOT NULL DEFAULT '',
            password    TEXT        NOT NULL DEFAULT '',
            database    TEXT        NOT NULL DEFAULT '',
            priority    INTEGER     NOT NULL,
            locked      BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # runtime_instances — removed 2026-08-28 (UI-075, UI-076).
    #
    # A Common Name, a URL and a bearer token, with no identity and no
    # permission model in either direction — and nothing ever dereferenced a
    # row. It was listed, saved, labelled and counted, and that was all.
    #
    # Contacts reaches another install's nodes already, whether that install is
    # yours or a friend's, with a username, a wallet address, a shared secret,
    # HMAC-signed packets and a permission check on both sides. A peer is a
    # peer; two paths to the same place, one of them weaker, is one too many.
    #
    # Dropped only when empty. An install that somehow has rows keeps them and
    # its table, because deleting somebody's configuration to tidy up a schema
    # is not a trade this project makes.
    textwrap.dedent("""        DO $$
        BEGIN
            -- Nested, not ANDed. PL/pgSQL evaluates a boolean expression as a
            -- single SQL query, so "table exists AND SELECT 1 FROM it" plans
            -- the inner SELECT even when the table does not exist -- which
            -- raises on every fresh install, where it never existed at all.
            IF EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'runtime_instances') THEN
                IF NOT EXISTS (SELECT 1 FROM runtime_instances) THEN
                    DROP TABLE runtime_instances;
                END IF;
            END IF;
        END $$;
    """),
    # ------------------------------------------------------------------
    # auth_credentials — credentials for various protocols (Milestone 25)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS auth_credentials (
            id          TEXT        PRIMARY KEY,
            name        TEXT        NOT NULL,
            auth_type   TEXT        NOT NULL, -- 'ssh', 'rdp', 'api_key', 'oauth'
            config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            priority    INTEGER     NOT NULL,
            locked      BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # firewall_contacts — trusted peer contacts and access control (Milestone 26)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS firewall_contacts (
            id            TEXT        PRIMARY KEY, -- cryptographic peer address
            name          TEXT        NOT NULL,
            access_rights JSONB       NOT NULL DEFAULT '{}'::jsonb, -- e.g. {"allowed_categories": ["rp"], "clearance_level": 5}
            priority      INTEGER     NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # tasks — long-form and queue execution tasks (Milestone 1)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS tasks (
            id           TEXT        PRIMARY KEY,
            parent_id    TEXT        REFERENCES tasks(id) ON DELETE SET NULL,
            description  TEXT        NOT NULL,
            status       TEXT        NOT NULL DEFAULT 'pending',
            dependencies TEXT[]      NOT NULL DEFAULT '{}',
            payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            result       JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='tasks' AND column_name='assigned_agent_id'
            ) THEN
                ALTER TABLE tasks ADD COLUMN assigned_agent_id TEXT;
                ALTER TABLE tasks ADD COLUMN model_override JSONB;
                ALTER TABLE tasks ADD COLUMN worktree_path TEXT;
                ALTER TABLE tasks ADD COLUMN branch_name TEXT;
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # task_event_store — append-only execution log (Milestone 1)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS task_event_store (
            id         BIGSERIAL   PRIMARY KEY,
            task_id    TEXT        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            event_type TEXT        NOT NULL,
            payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_task_event_store_task_id ON task_event_store(task_id);
    """),
    # ------------------------------------------------------------------
    # goal_decompositions — cache for semantic decomposition (Milestone 11)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS goal_decompositions (
            id          BIGSERIAL   PRIMARY KEY,
            goal        TEXT        NOT NULL,
            tasks_json  JSONB       NOT NULL,
            prompt_hash TEXT        NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_goal_decompositions_goal ON goal_decompositions(goal);
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='goal_decompositions' AND column_name='prompt_hash'
            ) THEN
                ALTER TABLE goal_decompositions ADD COLUMN prompt_hash TEXT NOT NULL DEFAULT '';
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # profiles — human, agent, project, organization, asset, workflow, memory profiles
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS profiles (
            id          TEXT        PRIMARY KEY,
            type        TEXT        NOT NULL, -- 'human', 'agent', 'project', 'organization', 'asset', 'workflow', 'memory'
            name        TEXT        NOT NULL,
            username    TEXT        NOT NULL DEFAULT '',
            config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_profiles_type ON profiles(type);
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_profiles_username ON profiles(username);
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_profiles_updated_at'
            ) THEN
                CREATE TRIGGER trg_profiles_updated_at
                BEFORE UPDATE ON profiles
                FOR EACH ROW EXECUTE FUNCTION tala_set_updated_at();
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # contacts – unique blockchain wallet mapped sharing contacts
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS contacts (
            username       TEXT        PRIMARY KEY,
            wallet_address TEXT        UNIQUE NOT NULL,
            endpoint_url   TEXT        NOT NULL DEFAULT '',
            shared_secret  TEXT        NOT NULL DEFAULT '',
            permissions    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS endpoint_url TEXT NOT NULL DEFAULT '';
    """),
    textwrap.dedent("""\
        ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS shared_secret TEXT NOT NULL DEFAULT '';
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_contacts_wallet ON contacts(wallet_address);
    """),
    # Conversations: the same object whatever the participants are.
    #
    # User to agent, user to user, users to users, users to agent, agents to
    # agents -- five shapes Steven named, and one model. A participant is an
    # identity: a peer username, or a local agent id. A message belongs to
    # the conversation rather than to a pair, which is the whole difference
    # from p2p_messages (peer_username + direction, structurally 1:1).
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT        PRIMARY KEY,
            title       TEXT        NOT NULL DEFAULT '',
            created_by  TEXT        NOT NULL DEFAULT '',
            -- 'joined' once a person decided to be in it. A conversation
            -- somebody was invited to is not one they are in: the hybrid
            -- rule approves the ROOM, so there has to be a moment where that
            -- happens.
            status      TEXT        NOT NULL DEFAULT 'invited',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS conversation_participants (
            conversation_id  TEXT        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            participant      TEXT        NOT NULL,
            participant_kind TEXT        NOT NULL DEFAULT 'user',
            joined_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (conversation_id, participant)
        );
    """),
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS conversation_messages (
            id              BIGSERIAL   PRIMARY KEY,
            conversation_id TEXT        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            sender          TEXT        NOT NULL,
            body            TEXT        NOT NULL,
            -- Whether the sender was an approved contact WHEN THIS ARRIVED.
            -- Stored rather than computed at read time: it is a fact about
            -- the message, and approving somebody later should not silently
            -- rewrite what was true when they said it.
            sender_approved BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_conversation_messages_convo
            ON conversation_messages(conversation_id, created_at);
    """),
    # Whether a node carrying code is allowed to run it.
    #
    # Keyed by node id AND content hash, which is the load-bearing part:
    # admitting a node admits *that code*. Edit a line and the hash moves, the
    # admission no longer matches, and the node is inert until somebody looks
    # at what changed. Keyed by id alone, "admitted once" would mean
    # "admitted forever, including for code substituted afterwards".
    #
    # Permission lives here rather than in the node because every field
    # inside a node is written by whoever wrote the node -- which, in the
    # case this exists for, is not the person running it.
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS node_admissions (
            node_id      TEXT        NOT NULL,
            content_hash TEXT        NOT NULL,
            origin       TEXT        NOT NULL DEFAULT 'local',
            admitted     BOOLEAN     NOT NULL DEFAULT FALSE,
            review       TEXT        NOT NULL DEFAULT '',
            reviewed_by  TEXT        NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (node_id, content_hash)
        );
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_node_admissions_node ON node_admissions(node_id);
    """),
    # Whether a human has decided about this contact yet.
    #
    # 'approved' is the default so that every contact already on disk keeps
    # working: those rows exist because the operator added them deliberately,
    # and a migration that silently suspended them would be a migration that
    # broke every existing peer. Only self-enrolment writes 'pending'.
    textwrap.dedent("""\
        ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved';
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    """),
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS p2p_sessions (
            peer_username TEXT        PRIMARY KEY,
            wallet_address TEXT       NOT NULL,
            endpoint_url   TEXT       NOT NULL DEFAULT '',
            status         TEXT       NOT NULL DEFAULT 'pending',
            handshake_json JSONB      NOT NULL DEFAULT '{}'::jsonb,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_message_at TIMESTAMPTZ
        );
    """),
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS p2p_messages (
            id             BIGSERIAL   PRIMARY KEY,
            peer_username  TEXT        NOT NULL,
            direction      TEXT        NOT NULL,
            message_json   JSONB       NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_contacts_updated_at'
            ) THEN
                CREATE TRIGGER trg_contacts_updated_at
                BEFORE UPDATE ON contacts
                FOR EACH ROW EXECUTE FUNCTION tala_set_updated_at();
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # access_grants — the one grant model (UI-062, UI-101, UI-118)
    #
    # Steven's rule is "by invite only": identity and access are separate, a
    # grant is per resource, and nothing is permitted by default. The owner is
    # implicit and unrestricted and has no rows here -- the absence of a row
    # for the owner is the point, not an omission.
    #
    # constraints carries the rest of what he specified: "that agent can only
    # access a certain search API, on a certain port, at a single allocated
    # time" -- ports, hosts, hours and an expiry.
    # ------------------------------------------------------------------
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS access_grants (
            id             TEXT        PRIMARY KEY,
            principal_kind TEXT        NOT NULL,
            principal_id   TEXT        NOT NULL,
            resource       TEXT        NOT NULL,
            permission     TEXT        NOT NULL DEFAULT 'read',
            constraints    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            granted_by     TEXT        NOT NULL DEFAULT 'owner',
            note           TEXT        NOT NULL DEFAULT '',
            granted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (principal_kind, principal_id, resource, permission)
        );
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_access_grants_lookup
            ON access_grants(principal_kind, principal_id, resource);
    """),

    # chain_ledger — which chain a transaction belongs to, and whether the
    # Real chain has actually accepted it yet.
    #
    # Steven's split: "The Ghost chain is only viewable locally. The Real chain
    # is viewable by all on the chain. They are meant to be separate."
    #
    # The distinction previously existed only as a value computed in
    # token_counter and written to a logger.debug line (UI-023); nothing
    # persisted it, so no transaction recorded which chain it was on. This is
    # that record. It is an index over the chains, not a second copy of them:
    # the Ghost chain itself remains blockchain/data/chain.json.
    #
    # The key is (chain, entry_id), not entry_id alone. One external event is
    # written to both chains and keeps one entry id across them, so a global
    # key means the Ghost row swallows the Real one and ON CONFLICT DO NOTHING
    # discards it without a word. That is how it first behaved: reconcile
    # reported learning four transactions and wrote none of them, because the
    # backfill already held those entry ids as Ghost.
    #
    # entry_id is synthetic and tx_id is not unique, which looks redundant and
    # is not. In the existing chain the transaction "id" is a *document*
    # identity: 2,748 transactions carry only 185 distinct ids, because
    # re-anchoring a node reuses the node's id -- settings-workflows alone
    # appears 174 times. Keying on it collapsed 2,748 entries into 185 and lost
    # the history. Only the block position is unique, so the key is synthetic
    # and tx_id is kept as what it actually is: the thing the transaction is
    # about, repeated legitimately every time that thing changes.
    #
    # pending is the offline queue. An external interaction that happens with
    # no connection is held here and written on reconnect, so the Real chain
    # stays complete rather than silently missing whatever happened offline.
    # ------------------------------------------------------------------
    # Two earlier shapes of this table were wrong in the same way -- a key that
    # merged rows which are not the same row. The first keyed on the
    # transaction id, a document identity, and collapsed 2,748 entries into
    # 185. The second keyed on entry_id alone and let a Ghost row swallow its
    # Real counterpart. Drop anything not keyed on (chain, entry_id); the rows
    # are an index the backfill rebuilds, so nothing is lost with them.
    textwrap.dedent("""        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'chain_ledger')
               AND NOT EXISTS (
                   SELECT 1
                     FROM information_schema.key_column_usage k
                     JOIN information_schema.table_constraints c
                       ON c.constraint_name = k.constraint_name
                    WHERE k.table_name = 'chain_ledger'
                      AND c.constraint_type = 'PRIMARY KEY'
                      AND k.column_name = 'chain') THEN
                DROP TABLE chain_ledger;
            END IF;
        END $$;
    """),
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS chain_ledger (
            entry_id     TEXT        NOT NULL,
            tx_id        TEXT        NOT NULL,
            chain        TEXT        NOT NULL,
            type         TEXT        NOT NULL,
            counterparty TEXT        NOT NULL DEFAULT '',
            document     TEXT        NOT NULL DEFAULT '',
            metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
            occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            pending      BOOLEAN     NOT NULL DEFAULT FALSE,
            remote_id    TEXT        NOT NULL DEFAULT '',
            written_at   TIMESTAMPTZ,
            -- 'local' for what this machine did, 'peer' for what reconcile
            -- pulled back from the shared chain. Without it "everything on the
            -- Real chain" and "everything I put on the Real chain" are the
            -- same query, and they are not the same question.
            origin       TEXT        NOT NULL DEFAULT 'local',
            PRIMARY KEY (chain, entry_id)
        );
    """),
    textwrap.dedent("""        ALTER TABLE chain_ledger
            ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'local';
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_chain_ledger_recent
            ON chain_ledger(chain, occurred_at DESC);
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_chain_ledger_tx
            ON chain_ledger(tx_id);
    """),
    textwrap.dedent("""        CREATE INDEX IF NOT EXISTS idx_chain_ledger_pending
            ON chain_ledger(occurred_at) WHERE pending;
    """),

    # chain_key_offers — keys that have been obtained but not yet trusted.
    #
    # Fetching a key and trusting it are two different acts, and collapsing
    # them is how key distribution turns into "admit whatever the network
    # says". A key arrives here; it becomes an authority only when the owner
    # says so, having compared the fingerprint over a channel neither node
    # controls.
    #
    # proven records whether the holder demonstrated possession of the private
    # key by signing a nonce we chose. That stops a directory advertising
    # somebody else's key -- it does not establish *identity*, which no amount
    # of network traffic can, and which is what the fingerprint comparison is
    # for. Both facts are recorded because they answer different questions.
    # ------------------------------------------------------------------
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS chain_key_offers (
            fingerprint TEXT        PRIMARY KEY,
            public_key  TEXT        NOT NULL,
            label       TEXT        NOT NULL DEFAULT '',
            source      TEXT        NOT NULL DEFAULT '',
            proven      BOOLEAN     NOT NULL DEFAULT FALSE,
            status      TEXT        NOT NULL DEFAULT 'offered',
            note        TEXT        NOT NULL DEFAULT '',
            offered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at  TIMESTAMPTZ
        );
    """),

    # app_settings — small, single-purpose settings that must not be lost
    # inside a larger blob.
    #
    # A key/value row rather than another JSON column on an existing table:
    # the profile data appeared to vanish because one save overwrote a whole
    # config object (UI-056), and the environment preset is exactly the kind of
    # value that would go the same way.
    # ------------------------------------------------------------------
    textwrap.dedent("""        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT        PRIMARY KEY,
            value      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),

    # heartbeats — proactive cron-scheduled autonomous goals
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS heartbeats (
            id          TEXT        PRIMARY KEY,
            name        TEXT        NOT NULL,
            cron_expr   TEXT        NOT NULL,
            goal_text   TEXT        NOT NULL,
            enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
            last_run_at TIMESTAMPTZ,
            last_status TEXT        NOT NULL DEFAULT 'pending',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_heartbeats_enabled ON heartbeats(enabled);
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_heartbeats_updated_at'
            ) THEN
                CREATE TRIGGER trg_heartbeats_updated_at
                BEFORE UPDATE ON heartbeats
                FOR EACH ROW EXECUTE FUNCTION tala_set_updated_at();
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # chat_channels — messaging gateway adapters (Discord, Telegram, …)
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS chat_channels (
            id          TEXT        PRIMARY KEY,
            platform    TEXT        NOT NULL,
            name        TEXT        NOT NULL DEFAULT '',
            enabled     BOOLEAN     NOT NULL DEFAULT FALSE,
            config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # conversation_turns — full history of interactions
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id          BIGSERIAL   PRIMARY KEY,
            session_id  TEXT        NOT NULL,
            role        TEXT        NOT NULL,
            content     TEXT        NOT NULL DEFAULT '',
            tool_calls  JSONB       NOT NULL DEFAULT '[]'::jsonb,
            metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_session
            ON conversation_turns (session_id);
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_created
            ON conversation_turns (created_at DESC);
    """),

    # ------------------------------------------------------------------
    # memory_index — unified text index for sessions, tasks, nodes, etc.
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS memory_index (
            id            BIGSERIAL   PRIMARY KEY,
            source_type   TEXT        NOT NULL, -- 'session', 'task', 'node', 'workflow', 'profile'
            ref_id        TEXT        NOT NULL,
            title         TEXT        NOT NULL DEFAULT '',
            content_text  TEXT        NOT NULL DEFAULT '',
            tsvector_col  TSVECTOR,
            trust_label   TEXT        NOT NULL DEFAULT 'recalled',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_memory_index_source
            ON memory_index (source_type);
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_memory_index_ref
            ON memory_index (ref_id);
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_memory_index_fts 
            ON memory_index USING GIN (tsvector_col);
    """),
    textwrap.dedent("""\
        CREATE OR REPLACE FUNCTION tala_update_memory_tsvector()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            NEW.tsvector_col = to_tsvector('english', COALESCE(NEW.title, '') || ' ' || COALESCE(NEW.content_text, ''));
            RETURN NEW;
        END;
        $$;
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_memory_index_tsvector'
            ) THEN
                CREATE TRIGGER trg_memory_index_tsvector
                BEFORE INSERT OR UPDATE OF title, content_text ON memory_index
                FOR EACH ROW EXECUTE FUNCTION tala_update_memory_tsvector();
            END IF;
        END;
        $$;
    """),

    # ------------------------------------------------------------------
    # research_results — caches web/local snippets and trust tags
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS research_results (
            id            TEXT        PRIMARY KEY,
            session_id    TEXT        NOT NULL,
            source_type   TEXT        NOT NULL,
            title         TEXT        NOT NULL DEFAULT '',
            snippet       TEXT        NOT NULL DEFAULT '',
            source_url    TEXT        NOT NULL DEFAULT '',
            summary       TEXT        NOT NULL DEFAULT '',
            score         FLOAT       NOT NULL DEFAULT 0.0,
            trust_label   TEXT        NOT NULL DEFAULT 'external',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_research_results_session
            ON research_results (session_id);
    """),

    # ------------------------------------------------------------------
    # memory_recall_events — logs injections and retrieved evidence
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS memory_recall_events (
            id               BIGSERIAL   PRIMARY KEY,
            query            TEXT        NOT NULL,
            recalled_id      TEXT        NOT NULL,
            is_injected      BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # graph_nodes — execution graph nodes for Graph-Engineered tasks
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id               TEXT        PRIMARY KEY,
            task_id          TEXT        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            node_type        TEXT        NOT NULL,
            description      TEXT        NOT NULL,
            status           TEXT        NOT NULL DEFAULT 'pending',
            dependencies     TEXT[]      NOT NULL DEFAULT '{}',
            inputs           JSONB       NOT NULL DEFAULT '{}'::jsonb,
            outputs          JSONB       NOT NULL DEFAULT '{}'::jsonb,
            success_criteria JSONB       NOT NULL DEFAULT '{}'::jsonb,
            retry_count      INTEGER     NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE INDEX IF NOT EXISTS idx_graph_nodes_task_id ON graph_nodes(task_id);
    """),
    textwrap.dedent("""\
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_graph_nodes_updated_at'
            ) THEN
                CREATE TRIGGER trg_graph_nodes_updated_at
                BEFORE UPDATE ON graph_nodes
                FOR EACH ROW EXECUTE FUNCTION tala_set_updated_at();
            END IF;
        END;
        $$;
    """),
    # ------------------------------------------------------------------
    # port_allocations — Fleet execution dynamic port mapping
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS port_allocations (
            task_id      TEXT        PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            port         INTEGER     NOT NULL UNIQUE,
            allocated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    # ------------------------------------------------------------------
    # auto_eval_workflow_runs – cross-turn cooldown for the Auto-Eval Gate.
    # Keyed by workflow CONTENT hash (not node id or file path) so that
    # multiple near-duplicate node files pointing at functionally identical
    # workflow JSON are recognized as "the same workflow" and don't each
    # independently re-trigger auto-execution.
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS auto_eval_workflow_runs (
            content_hash     TEXT        PRIMARY KEY,
            workflow_name    TEXT        NOT NULL,
            last_executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            execution_count  INTEGER     NOT NULL DEFAULT 1
        );
    """),
    # ------------------------------------------------------------------
    # Swarm run bookkeeping (sdk/agent/swarm_engine.py).
    #
    # Every branch of a fan-out swarm races every other branch, so the join
    # barrier, the in-flight count and the one-shot finalisation claim all
    # have to be single atomic statements. They deliberately do NOT live in
    # tasks.payload: a jsonb type codec is registered on every connection
    # (_configure_connection) while callers pass json.dumps(...) strings, so
    # task payloads are stored double-encoded — a JSON *string* containing
    # JSON, which is why every reader in this codebase does
    # `if isinstance(payload, str): json.loads(payload)`. jsonb_set and the
    # -> operators cannot address anything inside a scalar like that, so
    # swarm state gets its own properly-typed rows instead.
    # ------------------------------------------------------------------
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS swarm_run_state (
            parent_task_id TEXT        PRIMARY KEY,
            in_flight      INTEGER     NOT NULL DEFAULT 0,
            finalised      BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """),
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS swarm_join_arrivals (
            parent_task_id TEXT        NOT NULL,
            to_node        TEXT        NOT NULL,
            arrived        TEXT[]      NOT NULL DEFAULT '{}',
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (parent_task_id, to_node)
        );
    """),
    textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS swarm_gate_retries (
            parent_task_id TEXT    NOT NULL,
            rule_name      TEXT    NOT NULL,
            attempts       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (parent_task_id, rule_name)
        );
    """),
]



# ---------------------------------------------------------------------------
# Invariant guard
# ---------------------------------------------------------------------------

def assert_postgres_authority(caller: str = "") -> None:
    """
    Raise :class:`RuntimeError` if invoked from a mem0 context.

    Every write path in this module calls this to enforce the architectural
    invariant: PostgreSQL is the *only* durable authority.  mem0 is a
    read-only semantic cache and MUST NEVER be used for canonical state.

    Parameters
    ----------
    caller:
        Descriptive name of the calling site (used in the error message).
    """
    import inspect
    frame = inspect.currentframe()
    try:
        # Walk up the call stack looking for the mem0 context token
        while frame is not None:
            if _MEM0_CONTEXT_TOKEN in frame.f_locals:
                raise RuntimeError(
                    f"[TALA INVARIANT VIOLATION] {caller or 'unknown'} attempted a "
                    f"durable write from inside a mem0 context.  "
                    f"All durable state MUST flow through PostgreSQL (db.py).  "
                    f"mem0 is a READ-ONLY semantic cache."
                )
            frame = frame.f_back
    finally:
        del frame  # prevent reference cycles


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------

async def init_pool() -> Pool:
    """
    Initialise the asyncpg connection pool and apply DDL migrations.

    Returns the pool for optional direct use; also stored as module state
    accessible via :func:`get_pool`.
    """
    global _pool

    if _pool is not None:
        logger.debug("db.init_pool called but pool already exists — skipping")
        return _pool

    db_url = settings.DB_URL
    logger.info("Connecting to PostgreSQL: %s", _redact_url(db_url))

    try:
        import asyncio
        _pool = await asyncio.wait_for(
            asyncpg.create_pool(
                dsn=db_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
                # Ensure JSON/JSONB columns come back as dicts, not strings
                init=_configure_connection,
            ),
            timeout=15.0
        )
    except asyncio.TimeoutError as exc:
        logger.error("Timeout establishing connection pool to PostgreSQL. Is the database overloaded or connection limit reached?")
        raise RuntimeError("Database connection timed out during initialization.") from exc

    logger.info("PostgreSQL pool established (min=2 max=10)")
    await _run_migrations()
    return _pool


async def close_pool() -> None:
    """Drain and close the connection pool gracefully."""
    global _pool
    if _pool is None:
        return
    logger.info("Closing PostgreSQL pool …")
    await _pool.close()
    _pool = None
    logger.info("PostgreSQL pool closed")


def get_pool() -> Pool:
    """
    Return the active connection pool.

    Raises
    ------
    RuntimeError
        If :func:`init_pool` has not been called yet.
    """
    if _pool is None:
        raise RuntimeError(
            "Database pool is not initialised.  "
            "Ensure init_pool() is awaited during server startup."
        )
    return _pool


# ---------------------------------------------------------------------------
# Public async query helpers
# ---------------------------------------------------------------------------

async def execute(query: str, *args: Any, caller: str = "") -> str:
    """
    Execute a DML statement (INSERT / UPDATE / DELETE / DDL).

    Parameters
    ----------
    query:
        Parameterised SQL string with ``$1``, ``$2`` … placeholders.
    *args:
        Positional parameters bound to the placeholders.
    caller:
        Optional descriptor used in invariant-violation messages.

    Returns
    -------
    str
        The PostgreSQL command status tag, e.g. ``'INSERT 0 1'``.
    """
    assert_postgres_authority(caller or query[:60])
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(query, *args)
    return result


async def fetch(query: str, *args: Any) -> list[Record]:
    """
    Run a SELECT and return all matching rows as a list of :class:`asyncpg.Record`.

    Each record supports attribute-style access: ``row["column_name"]``.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return rows


async def fetchrow(query: str, *args: Any) -> Record | None:
    """
    Run a SELECT and return the first row, or ``None`` if no rows match.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *args)
    return row


async def fetchval(query: str, *args: Any, column: int = 0) -> Any:
    """
    Run a SELECT and return a single scalar value from *column* of the first row.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(query, *args, column=column)
    return value


async def try_claim_auto_eval_run(content_hash: str, workflow_name: str, cooldown_seconds: float) -> bool:
    """
    Atomically check-and-claim a cooldown slot for the Auto-Eval Gate.

    Returns True (and records the claim) if *content_hash* has never run or
    its cooldown has expired — the caller should proceed with execution.
    Returns False if another execution already claimed this content hash
    within the cooldown window — the caller should skip.

    Keyed by workflow content hash rather than node id or file path, so
    multiple node files pointing at duplicate/near-duplicate workflow JSON
    are recognized as "the same workflow" for cooldown purposes.
    """
    row = await fetchrow(
        """
        INSERT INTO auto_eval_workflow_runs (content_hash, workflow_name, last_executed_at, execution_count)
        VALUES ($1, $2, NOW(), 1)
        ON CONFLICT (content_hash) DO UPDATE
            SET last_executed_at = NOW(),
                execution_count = auto_eval_workflow_runs.execution_count + 1
            WHERE auto_eval_workflow_runs.last_executed_at < NOW() - ($3 * INTERVAL '1 second')
        RETURNING content_hash
        """,
        content_hash,
        workflow_name,
        cooldown_seconds,
    )
    return row is not None


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """
    Async context manager that provides a transactional connection.

    Usage::

        async with db.transaction() as conn:
            await conn.execute("INSERT INTO …", …)
            await conn.execute("UPDATE …", …)
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


# ---------------------------------------------------------------------------
# Convenience log helper (writes to agent_log table)
# ---------------------------------------------------------------------------

#: One spelling per level. Both halves of a split spelling look like a working
#: filter until you notice half the rows are missing.
_LOG_LEVEL_ALIASES = {
    "WARN": "WARNING",
    "ERR": "ERROR",
    "CRIT": "CRITICAL",
    "FATAL": "CRITICAL",
    "TRACE": "DEBUG",
}


async def log_to_db(
    level: str,
    message: str,
    trace: dict[str, Any] | None = None,
    tx_id: str | None = None,
) -> None:
    """
    Append a structured log entry to the ``agent_log`` table.

    This is a fire-and-forget helper; errors are swallowed and re-emitted
    to the Python logger so that a DB outage never silences the harness.
    """
    try:
        import json
        # WARN and WARNING were both in use -- 98 rows against 16,589 -- which
        # silently breaks any level filter, since a query for one misses the
        # other. Normalised on the way in so the column has one spelling per
        # level; existing rows are migrated by scripts/normalise_log_levels.py.
        level = _LOG_LEVEL_ALIASES.get(str(level or "").strip().upper(),
                                       str(level or "INFO").strip().upper())
        # NOT json.dumps'd here. The connection codec set in
        # _configure_connection already encodes every JSONB value with
        # json.dumps, so encoding first stored a JSON *string* rather than an
        # object: jsonb_typeof(trace) came back 'string' and every
        # trace->>'key' silently returned NULL.
        #
        # That is not theoretical. The Guardrails panel's "PROMPT AUDITS" tile
        # was rewritten to count trace->>'event_type' instead of grepping the
        # message text (UI-087), and it read 0 for exactly this reason -- the
        # same silent-zero the rewrite was meant to eliminate.
        trace_payload = trace or {}
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_log (level, message, trace, tx_id)
                VALUES ($1, $2, $3::jsonb, $4)
                """,
                level,
                message,
                trace_payload,
                tx_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write to agent_log: %s", exc)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _configure_connection(conn: asyncpg.Connection) -> None:
    """Connection init callback — configure codec for JSONB columns."""
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: __import__("json").dumps(v),
        decoder=lambda v: __import__("json").loads(v),
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda v: __import__("json").dumps(v),
        decoder=lambda v: __import__("json").loads(v),
        schema="pg_catalog",
        format="text",
    )


async def _ensure_embeddings_schema() -> None:
    """
    Create the ``embeddings`` table the VPS write gate reads.

    No migration ever created it. Installs that have one got it from a
    migration since removed, so the table survived by inheritance and its
    absence only showed up on a genuinely fresh database -- which is exactly
    what rebuilding talacore.app produced:

        Failed to seed approved VPS memory context:
        relation "embeddings" does not exist

    That is not cosmetic. ``VPSWriteStateMachine.validate`` decides whether a
    write to a connection node is allowed by looking for an approved context
    in this table, and with no table there is no approved context, so every
    such write is DENIED. A brand-new install could therefore never connect
    to the VPS, and the reason it could not was a missing table reported as a
    memory-context denial.

    Guarded rather than added to _DDL_STATEMENTS: pgvector may be absent, and
    the harness losing peer-write validation should be a loud warning naming
    the consequence, not a refusal to start. The extension and the table are
    attempted separately, so an install whose extension is already present
    still gets its table when the CREATE EXTENSION is refused -- that
    statement needs privileges the app's own role may not hold.
    """
    try:
        await execute("CREATE EXTENSION IF NOT EXISTS vector", caller="db._ensure_embeddings_schema")
    except Exception as exc:
        logger.warning(
            "Could not create the pgvector extension (%s). Continuing: it may "
            "already exist, or an administrator may need to add it.", exc,
        )

    try:
        await execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id              UUID        PRIMARY KEY,
                owner_kind      TEXT        NOT NULL,
                owner_id        UUID,
                chunk_index     INTEGER     NOT NULL DEFAULT 0,
                embedding_model TEXT        NOT NULL,
                content         TEXT        NOT NULL,
                content_hash    TEXT        NOT NULL,
                metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                embedding       vector(1536)
            );
            """,
            caller="db._ensure_embeddings_schema",
        )
        await execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_owner ON embeddings(owner_kind, owner_id);",
            caller="db._ensure_embeddings_schema",
        )
    except Exception as exc:
        logger.error(
            "Could not create the embeddings table (%s). Writes to a connection "
            "node will be DENIED, because the VPS write gate validates against "
            "approved contexts stored there. Install pgvector "
            "(e.g. postgresql-16-pgvector) and restart.",
            exc,
        )


async def seed_approved_vps_context() -> None:
    """
    NAME
           seed_approved_vps_context - Seed approved memory contexts into PG embeddings table
           
    SYNOPSIS
           await seed_approved_vps_context()
           
    DESCRIPTION
           Checks if the approved system memory context is already present in the
           embeddings table, and seeds it using our deterministic offline embedding if not.
    """
    try:
        from python.harness.vps_state_machine import generate_offline_embedding, vector_to_str
        import uuid
        import hashlib
        
        approved_text = "approved local-to-vps synchronization and ledger update"
        exists = await fetchrow("SELECT 1 FROM embeddings WHERE owner_kind = 'system-approved' AND content = $1", approved_text)
        if not exists:
            approved_emb = generate_offline_embedding(approved_text)
            await execute(
                """
                INSERT INTO embeddings (id, owner_kind, owner_id, chunk_index, embedding_model, content, content_hash, embedding, metadata)
                VALUES ($1, 'system-approved', $2, 0, 'offline-deterministic', $3, $4, $5::vector, '{}'::jsonb)
                """,
                uuid.uuid4(),
                uuid.UUID("00000000-0000-0000-0000-000000000000"),
                approved_text,
                hashlib.sha256(approved_text.encode('utf-8')).hexdigest(),
                vector_to_str(approved_emb)
            )
            logger.info("Successfully seeded approved VPS memory context into embeddings table.")
    except Exception as exc:
        logger.warning("Failed to seed approved VPS memory context: %s", exc)


async def _run_migrations() -> None:
    """Apply all DDL statements idempotently (CREATE IF NOT EXISTS)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        # Check and drop legacy graph_nodes if missing task_id column
        try:
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 
                    FROM information_schema.tables 
                    WHERE table_name='graph_nodes'
                );
            """)
            if table_exists:
                col_exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='graph_nodes' AND column_name='task_id'
                    );
                """)
                if not col_exists:
                    logger.warning("Legacy graph_nodes table detected (missing task_id). Dropping and recreating it.")
                    await conn.execute("DROP TABLE IF EXISTS graph_nodes CASCADE;")
        except Exception as e:
            logger.warning("Failed to check/drop legacy graph_nodes table: %s", e)

        for stmt in _DDL_STATEMENTS:
            try:
                await conn.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                logger.error("Migration failed:\n%s\nError: %s", stmt, exc)
                raise
    logger.info("Database schema migrations applied successfully")
    try:
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE search_providers ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;")
            await conn.execute("ALTER TABLE git_providers ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;")
            await conn.execute("ALTER TABLE storage_providers ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;")
            await conn.execute("ALTER TABLE database_providers ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;")
            await conn.execute("ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;")
            await conn.execute("ALTER TABLE search_providers ADD COLUMN IF NOT EXISTS credential_id TEXT;")
            await conn.execute("ALTER TABLE git_providers ADD COLUMN IF NOT EXISTS credential_id TEXT;")
            await conn.execute("ALTER TABLE agent_log ADD COLUMN IF NOT EXISTS tx_id TEXT;")
    except Exception as exc:
        logger.warning("Failed to run dynamic migrations: %s", exc)
    await _ensure_embeddings_schema()
    try:
        await seed_approved_vps_context()
    except Exception as exc:
        logger.warning("Failed to seed approved VPS memory context after migrations: %s", exc)

    try:
        from sdk.agent.code_registry import CodeRegistry
        await CodeRegistry.seed_registry(pool)
    except Exception as exc:
        logger.warning("Failed to seed CodeRegistry: %s", exc)



def _redact_url(url: str) -> str:
    """Replace password in a DSN string with '***' for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if parsed.password:
            netloc = parsed.netloc.replace(parsed.password, "***")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:  # noqa: BLE001
        pass
    return url

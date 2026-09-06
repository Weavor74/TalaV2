"""
personas.py — choosing who Tala becomes.

Every fresh install becomes the same person. `db.py` seeds one agent row --
`tala-core`, "You are Tala, the core offline cognitive agent" -- marks it
active, and that is the end of it. Three persona nodes ship in `nodes/`, and
nothing offers them: they sit on disk as files a new user has no way to
reach.

So the unit here is the **node**, not a table row. A persona is an HTML-NN
like everything else Tala consumes, and adopting one means reading that node
and becoming what it describes. That choice is deliberate and load-bearing
for what comes next: distributing personas between installs later needs only
a different source for the node, not a different notion of what a persona is.
Fetch it from a peer instead of from disk and `adopt` is unchanged.

What a persona node carries, and what it becomes:

    node_id      -> agents.id
    title        -> agents.name
    role         -> agents.role
    config       -> agents.config   (system_prompt, birthdate, tag, ...)

Not every persona node is adoptable. `persona-tala-origin` declares
execution authority 0.0 and carries no system prompt: it is lore, context
for a persona rather than a persona itself. Adopting it would leave an agent
with nothing to say. `_is_adoptable` is what separates the two, and the
listing reports the ones it excluded rather than hiding them, because a
persona that silently fails to appear looks like a missing file.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Persona nodes are named persona-<id>.html, which is also how
#: settings_nn.py writes the mirror of an agent row.
NODE_PREFIX = "persona-"
NODE_TYPE = "persona"


def _nodes_dir():
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    return resolve_active_nodes_dir()


def _is_adoptable(state: dict[str, Any]) -> tuple[bool, str]:
    """
    Can this node be become, or is it only about someone who can?

    Returns (adoptable, why_not). A persona needs something to say: without
    a system prompt an adopted agent has no instructions, which is not a
    quiet degradation but an agent that behaves like no one at all.
    """
    config = state.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}
    config = config or {}

    if not str(config.get("system_prompt") or "").strip():
        return False, "carries no system prompt; it is context about a persona, not one"
    return True, ""


def _read_node(path) -> dict[str, Any] | None:
    from sdk.agent.parser import parse_file

    try:
        node = parse_file(str(path))
    except Exception as exc:
        log.warning("personas: could not parse %s: %s", path.name, exc)
        return None

    if (node.node_type or "").strip().lower() != NODE_TYPE:
        return None

    state = node.state_data or {}
    config = state.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}

    return {
        "id": node.node_id,
        "name": state.get("title") or node.node_id,
        "role": state.get("role") or "",
        "description": state.get("description") or "",
        "tags": state.get("tags") or [],
        "config": config or {},
        "execution_authority": node.execution_authority,
        "source": path.name,
    }


def available() -> dict[str, Any]:
    """
    Every persona node on disk, split into what can be adopted and what cannot.

    The excluded ones are returned with a reason rather than filtered out.
    A persona that simply fails to appear is indistinguishable from a
    missing file, and this codebase's recurring defect is exactly that --
    an empty result that cannot be told apart from a broken lookup.
    """
    directory = _nodes_dir()
    personas: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []

    if not directory.exists():
        return {"personas": [], "unavailable": [], "reason": f"no nodes directory at {directory}"}

    for path in sorted(directory.glob(f"{NODE_PREFIX}*.html")):
        node = _read_node(path)
        if node is None:
            continue
        ok, why_not = _is_adoptable(node)
        if ok:
            personas.append(node)
        else:
            unavailable.append({"id": node["id"], "name": node["name"], "reason": why_not})

    return {"personas": personas, "unavailable": unavailable}


async def adopt(persona_id: str) -> dict[str, Any]:
    """
    Become the persona described by a node.

    Writes the agent row and makes it the active one, in a single
    transaction: an install that adopted a persona but did not activate it
    would look like the choice was ignored.

    The row is upserted rather than inserted, so re-adopting refreshes an
    agent from its node instead of failing on a primary key -- and so the
    later network case, where a node arrives with an id already present,
    behaves the same way.
    """
    from python.harness import db

    directory = _nodes_dir()
    path = directory / f"{NODE_PREFIX}{persona_id}.html"
    if not path.exists():
        return {"adopted": False, "reason": f"no persona node {path.name}"}

    node = _read_node(path)
    if node is None:
        return {"adopted": False, "reason": f"{path.name} is not a persona node"}

    ok, why_not = _is_adoptable(node)
    if not ok:
        return {"adopted": False, "reason": why_not}

    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO agents (id, name, type, role, is_active, config)
                VALUES ($1, $2, 'standalone', $3, TRUE, $4::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    role = EXCLUDED.role,
                    config = EXCLUDED.config,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                node["id"], node["name"], node["role"], json.dumps(node["config"]),
            )
            # Exactly one active agent, and the transaction is what keeps
            # that true: deactivating first and failing to activate would
            # leave the install with no persona at all.
            await conn.execute("UPDATE agents SET is_active = FALSE WHERE id <> $1", node["id"])

    log.info("personas: adopted %s (%s) from %s", node["id"], node["name"], node["source"])
    return {"adopted": True, "persona": {k: node[k] for k in ("id", "name", "role", "description")}}


async def active() -> dict[str, Any]:
    """Who this install currently is, and whether that was ever chosen."""
    from python.harness import db

    row = await db.fetchrow(
        "SELECT id, name, role, created_at, updated_at FROM agents WHERE is_active = TRUE LIMIT 1"
    )
    if not row:
        return {"active": None, "chosen": False}

    # The seeded default is the one nobody picked. Distinguishing it is what
    # lets a first run offer the choice instead of assuming silence means
    # agreement.
    chosen = row["id"] != "tala-core" or row["updated_at"] != row["created_at"]
    return {
        "active": {"id": row["id"], "name": row["name"], "role": row["role"]},
        "chosen": bool(chosen),
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
#
# A persona's past is an HTML-NN, like everything else the harness consumes.
# `background_file` on the agent config names it, the prompt assembler reads
# that node's semantic_memory, and it arrives as
# "[Personality Background / History Context]".
#
# Two ways to get one, and they differ only in who writes the prose:
#
#   hand-written  the user authors the node themselves
#   generated     the user describes a past and a model drafts it
#
# Generated history is authored history. Steven's decision: it is written
# once, and from then on it is a normal node the user owns and edits. Not
# regenerated as the persona evolves -- a past that keeps changing underneath
# somebody is not a past, and T.A.L.A.'s own origin node says exactly why:
# "none of it is something I tell casually -- it is the ground under the way
# I speak, not an anecdote I reach for."
#
# So `draft_history` returns prose and writes nothing. Saving is a separate,
# explicit step. That split is deliberate: `create_learning_node` had to grow
# an `assess_learning_quality` gate because auto-generated nodes flooded the
# wiki -- 3 of 17 were real -- and the lesson is that a generator which mints
# nodes on its own produces work somebody else has to clean up.

HISTORY_NODE_PREFIX = "history-"

_HISTORY_SYSTEM_PROMPT = """\
You write the private history of a character, in their own first-person voice.

Write what happened to them and what it left behind: the events, the people,
the things they carry. Not a character sheet, not traits, not a summary of
their personality -- a past.

Rules:
- First person, past tense, plain prose. No headings, no bullets, no preamble.
- Concrete and specific. Name places, people and moments.
- Nothing about the present, and no reference to being an AI, an assistant,
  or to whoever they now work with.
- 200-400 words.
"""


def _history_node_id(persona_id: str) -> str:
    return f"{HISTORY_NODE_PREFIX}{persona_id}"


async def draft_history(persona_id: str, description: str, name: str = "") -> dict[str, Any]:
    """
    Draft a past from a description. Writes nothing.

    The caller shows the prose to the person whose persona it is and saves it
    only if they accept, so nothing reaches nodes/ that a human has not read.
    """
    description = str(description or "").strip()
    if not description:
        return {"drafted": False, "reason": "a description is required to write a history from"}

    prompt = (
        f"Write the private history of {name or persona_id}.\n\n"
        f"What is known about them:\n{description}\n"
    )

    try:
        from python.harness.server import handle_inference_generate

        raw = await handle_inference_generate(
            {
                "prompt": prompt,
                "stream": False,
                "raw_prompt": True,
                "system_prompt": _HISTORY_SYSTEM_PROMPT,
                "max_tokens": 700,
            },
            ws=None,
            req_id="persona-history",
        )
        text = (json.loads(raw).get("result") or {}).get("text", "").strip()
    except Exception as exc:
        log.warning("personas: history generation failed for %s: %s", persona_id, exc)
        return {"drafted": False, "reason": f"the model could not be reached: {exc}"}

    if not text:
        return {"drafted": False, "reason": "the model returned nothing"}

    return {
        "drafted": True,
        "persona_id": persona_id,
        "node_id": _history_node_id(persona_id),
        "history": text,
        # Said plainly, because a draft shown without this reads as a fact.
        "note": "Nothing has been saved. Edit this freely; it becomes yours when you save it.",
    }


async def save_history(persona_id: str, history: str, name: str = "") -> dict[str, Any]:
    """
    Write a history node and point the persona at it.

    Hand-written and generated history arrive here identically -- once saved
    there is no such thing as a generated node, only a node. Pointing the
    persona at it is part of the same step, because a history node nothing
    references is a file, not a memory.
    """
    from python.harness import db

    history = str(history or "").strip()
    if not history:
        return {"saved": False, "reason": "there is no history to save"}

    node_id = _history_node_id(persona_id)
    try:
        from sdk.agent.documentation_nodes import create_documentation_node

        create_documentation_node(
            node_id=node_id,
            title=f"{name or persona_id} — history",
            doc_target=f"persona:{persona_id}",
            semantic_memory=history,
            node_type="persona-history",
            tags=["persona", "history", persona_id],
        )
    except Exception as exc:
        log.exception("personas: could not write the history node for %s", persona_id)
        return {"saved": False, "reason": str(exc)}

    row = await db.fetchrow("SELECT config FROM agents WHERE id = $1", persona_id)
    if row:
        config = row["config"]
        config = json.loads(config) if isinstance(config, str) else dict(config)
        config["background_file"] = f"{node_id}.html"
        await db.execute(
            "UPDATE agents SET config = $2::jsonb, updated_at = NOW() WHERE id = $1",
            persona_id, json.dumps(config), caller="personas.save_history",
        )

    log.info("personas: %s now remembers %s (%d chars)", persona_id, node_id, len(history))
    return {"saved": True, "node_id": node_id, "background_file": f"{node_id}.html"}

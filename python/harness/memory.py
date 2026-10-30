"""
memory.py — Tala Evidence Browser Backend
=========================================
Implements the JSON-RPC methods for memory search, prompt injection defense,
and unified research extraction.
"""

import json
import logging
import re
import uuid
from typing import Any
from datetime import datetime, timezone

from python.harness import db
from python.harness.search_manager import SearchManager
from sdk.agent.documentation_nodes import create_learning_documentation_node
from sdk.agent.memory.manager import get_manager
from python.harness.sync_manager import sync_manager

logger = logging.getLogger(__name__)

# Phrases that signal a durable lesson rather than ordinary prose.
#
# "note that" is deliberately absent. Unlike the rest of these it is pure
# connective tissue in normal explanation -- "note that the config lives in
# settings.json" is an aside, not a standing rule, and firing on it minted a
# permanent wiki page every time she explained something. The others here are
# directive: a person says "from now on" or "the rule is" when they mean it to
# stick.
#
# This is only the first of two filters. Whatever passes still has to clear
# assess_learning_quality() in sdk/agent/documentation_nodes.py, which rejects
# task restatements, completion boilerplate, and anything too thin to be worth
# a permanent node. That second gate is what does the heavy lifting, so these
# cues can stay generous without producing junk.
_LEARNING_CUE_PATTERNS = (
    re.compile(r"\bi learned\b", re.IGNORECASE),
    re.compile(r"\blearned that\b", re.IGNORECASE),
    re.compile(r"\bfrom now on\b", re.IGNORECASE),
    re.compile(r"\bgoing forward\b", re.IGNORECASE),
    re.compile(r"\bremember that\b", re.IGNORECASE),
    re.compile(r"\bthe rule is\b", re.IGNORECASE),
    re.compile(r"\btala should\b", re.IGNORECASE),
    re.compile(r"\bpreference is\b", re.IGNORECASE),
    re.compile(r"\bwe should remember\b", re.IGNORECASE),
    re.compile(r"\bfor future reference\b", re.IGNORECASE),
    re.compile(r"\blesson learned\b", re.IGNORECASE),
)


#: A reply that only acknowledges ("Got it ... I'll keep that in mind for future reference") states no lesson, but it often contains a cue
#: phrase. Found 2026-10-30: such a reply became a weight-1.0 learning node, a second copy of a fact the user later retired.
_ACKNOWLEDGEMENT = re.compile(r"^\W*(?:got\s+it|understood|noted|sure|okay|ok|alright|done|will\s+do|of\s+course|absolutely|certainly|roger|copy\s+that)\b",
                              re.IGNORECASE)


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def _is_learning_event(params: dict[str, Any], role: str, content: str) -> bool:
    explicit_event = params.get("learning_event")
    if isinstance(explicit_event, dict):
        return True
    if explicit_event:
        return True
    if params.get("force_learning_node"):
        return True
    if role != "assistant" or not content.strip():
        return False
    if params.get("memory_command") or _ACKNOWLEDGEMENT.match(content):
        return False  # the harness already stored a remembered / corrected / forgotten fact where it belongs, or she only acknowledged
    return any(pattern.search(content) for pattern in _LEARNING_CUE_PATTERNS)


def _extract_learning_topic(content: str, params: dict[str, Any]) -> str:
    for key in ("learning_topic", "topic", "canonical_topic", "lesson_title"):
        candidate = _coerce_text(params.get(key))
        if candidate:
            return candidate

    content_text = content.strip()
    if not content_text:
        return "Durable learning"

    cue_patterns = (
        r"(?:I learned|learned that|remember that|note that|from now on|going forward|the rule is|preference is|Tala should)\s*[:,\-]?\s*(.+)",
    )
    for pattern in cue_patterns:
        match = re.search(pattern, content_text, re.IGNORECASE | re.DOTALL)
        if match:
            fragment = re.split(r"[.!?\n]", match.group(1).strip(), maxsplit=1)[0].strip()
            if fragment:
                return fragment[:120]

    first_sentence = re.split(r"[.!?\n]", content_text, maxsplit=1)[0].strip()
    return (first_sentence[:120] or "Durable learning")


def _build_learning_node_payload(
    *,
    session_id: str,
    role: str,
    content: str,
    tool_calls: list[Any],
    params: dict[str, Any],
) -> dict[str, Any] | None:
    if not _is_learning_event(params, role, content):
        return None

    explicit_event = params.get("learning_event")
    if isinstance(explicit_event, dict):
        payload = dict(explicit_event)
    else:
        payload = {}

    topic = _coerce_text(payload.get("topic")) or _extract_learning_topic(content, params)
    summary = _coerce_text(payload.get("summary")) or content.strip()
    canonical_node_id = _coerce_text(payload.get("canonical_node_id")) or _coerce_text(
        params.get("canonical_node_id")
    )

    details = payload.get("details")
    if not isinstance(details, list):
        details = []
    normalized_details = [str(item).strip() for item in details if str(item).strip()]
    if not normalized_details:
        normalized_details = [
            f"Session: {session_id}",
            f"Role: {role}",
            f"Tool calls: {len(tool_calls)}",
        ]
        if content.strip():
            normalized_details.append("Learning cue captured from the completed assistant turn.")

    source_files = payload.get("source_files")
    if not isinstance(source_files, list):
        source_files = []

    references = payload.get("references")
    if not isinstance(references, list):
        references = []

    relationships = payload.get("relationships")
    if not isinstance(relationships, list):
        relationships = []

    tags = payload.get("tags")
    if not isinstance(tags, list):
        tags = []

    node_id = _coerce_text(payload.get("node_id")) or None

    return {
        "topic": topic,
        "summary": summary,
        "canonical_node_id": canonical_node_id or None,
        "details": normalized_details,
        "source_files": source_files,
        "references": references,
        "relationships": relationships,
        "tags": tags,
        "node_id": node_id,
    }

async def _current_emotion_label() -> str | None:
    """
    Tala's current emotional state as a plain label, or None.

    This used to be ``_inject_emotion_to_query``, which prepended
    ``[Emotional State: X] `` onto the search string. That is the wrong
    mechanism for this index: ``memory_index`` is Postgres full-text
    (``tsvector_col``), so concatenating the label into the query only adds
    tokens that appear in no stored document, diluting the real search terms
    and — for an exact-marker lookup — dropping the match entirely.

    Colouring recall by mood is a ranking concern, not a query-string concern.
    Returning the label instead of splicing it leaves that door open without
    corrupting the query.

    The bug never surfaced because the emotion lookup silently failed on every
    call, so the injection never actually happened.
    """
    from python.harness.chat_mode import is_emotional_mode

    if not is_emotional_mode():
        return None

    agent_id = None
    try:
        active_row = await db.fetchrow("SELECT id FROM agents WHERE is_active = TRUE")
        if active_row:
            agent_id = str(active_row["id"])
    except Exception as exc:
        logger.warning(f"Failed to fetch active agent: {exc}")

    if not agent_id:
        return None

    try:
        from python.harness.mcp_manager import mcp_manager
        emotion_result = await mcp_manager.call_tool("astro-emotion", "get_emotional_state", {"agent_id": agent_id})
        emotion_state = None
        if isinstance(emotion_result, dict):
            emotion_state = emotion_result.get("emotional_state") or emotion_result.get("value") or emotion_result
        else:
            emotion_state = emotion_result

        if emotion_state:
            if isinstance(emotion_state, dict):
                return emotion_state.get("primary_emotion") or None
            return str(emotion_state)
    except Exception as exc:
        logger.warning(f"Failed to fetch emotional state for search context: {exc}")
    return None


async def handle_memory_search(params: dict[str, Any]) -> dict[str, Any]:
    """
    Search all active semantic indexes (node contents, external memories, etc.)
    """
    query = params.get("query", "")
    # NOTE: learning nodes created via the auto-learning-cue path (see
    # _build_learning_node_payload / create_learning_documentation_node) are
    # indexed with source_type "documentation" (their HTML-NN node_type),
    # not "node" -- they must be in the default list or durable learning
    # events become permanently unrecallable by default.
    sources = params.get(
        "sources",
        ["session", "task", "node", "workflow", "profile", "documentation", "program-doc", "function-doc"],
    )
    limit = params.get("limit", 20)
    allow_private = params.get("allow_private", False)

    if not query:
        return {"results": []}

    # Her current mood is surfaced alongside the results rather than spliced
    # into the query string -- see _current_emotion_label for why.
    emotion_label = await _current_emotion_label()

    manager = get_manager()
    results = await manager.search(query, sources, limit, allow_private)

    # Log recall event for the top result
    if results:
        try:
            await db.execute(
                """INSERT INTO memory_recall_events (query, recalled_id, is_injected) VALUES ($1, $2, FALSE)""",
                query, str(results[0]["id"])
            )
        except Exception as e:
            logger.warning(f"Failed to log recall event: {e}")

    out: dict[str, Any] = {"results": results}
    if emotion_label:
        out["emotional_state"] = emotion_label
    return out

async def handle_memory_session_search(params: dict[str, Any]) -> dict[str, Any]:
    """
    Search specifically within conversation sessions.
    """
    query = params.get("query", "")
    limit = params.get("limit", 20)

    if not query:
        return {"results": []}

    emotion_label = await _current_emotion_label()

    manager = get_manager()
    results = await manager.session_search(query, limit)

    out: dict[str, Any] = {"results": results}
    if emotion_label:
        out["emotional_state"] = emotion_label
    return out



async def _browse_node_from_disk(ref_id: str) -> str | None:
    """
    Read an HTML Neural Node from REPO_ROOT/nodes when the DB row is missing.

    ``ref_id`` may be a bare node id (``concept-foo``), an id with ``.html``,
    or a basename. Returns None when no readable file matches.
    """
    from pathlib import Path

    from python.harness.config import settings

    raw = str(ref_id or "").strip()
    if not raw:
        return None
    nodes_dir = (settings.REPO_ROOT / "nodes").resolve()
    if not nodes_dir.is_dir():
        return None

    candidates: list[Path] = []
    name = Path(raw).name
    if name.endswith(".html"):
        candidates.append(nodes_dir / name)
    else:
        candidates.append(nodes_dir / f"{name}.html")
        candidates.append(nodes_dir / name)

    for path in candidates:
        try:
            resolved = path.resolve()
            # Path boundary (not startswith): nodes vs nodes_evil prefix hole.
            if not resolved.is_relative_to(nodes_dir):
                continue
            if resolved.is_file():
                text = resolved.read_text(encoding="utf-8", errors="replace")
                try:
                    from sdk.agent.parser import parse_html
                    node = parse_html(text)
                    payload = {
                        "id": getattr(node, "id", name),
                        "title": getattr(node, "title", None),
                        "type": getattr(node, "type", None),
                        "source": "disk",
                        "path": str(resolved),
                        "content": getattr(node, "content", None) or text,
                    }
                    for attr in ("state_data", "summary", "tags", "execution_authority"):
                        if hasattr(node, attr):
                            payload[attr] = getattr(node, attr)
                    return json.dumps(payload, indent=2, default=str)
                except Exception:
                    return json.dumps(
                        {"id": name, "source": "disk", "path": str(resolved), "html": text},
                        indent=2,
                    )
        except OSError:
            continue
    return None


async def handle_memory_browse(params: dict[str, Any]) -> dict[str, Any]:
    """
    Return surrounding context for a given reference (e.g., surrounding turns).
    """
    ref_id = params.get("ref_id")
    source_type = params.get("source_type")
    
    if source_type == "session":
        # Fetch conversation turns for this session
        sql = """
            SELECT role, content, created_at
            FROM conversation_turns
            WHERE session_id = $1
            ORDER BY created_at ASC
            LIMIT 50
        """
        rows = await db.fetch(sql, ref_id)
        context = "\n\n".join([f"[{r['created_at'].isoformat()}] {r['role'].upper()}: {r['content']}" for r in rows])
    elif source_type == "task":
        sql = """
            SELECT event_type, payload, created_at
            FROM task_event_store
            WHERE task_id = $1
            ORDER BY created_at ASC
            LIMIT 50
        """
        rows = await db.fetch(sql, ref_id)
        context = "\n".join([f"[{r['created_at'].isoformat()}] {r['event_type']}: {json.dumps(r['payload'])}" for r in rows])
    elif source_type == "node" or source_type == "neural-node":
        sql = "SELECT content_json FROM nodes WHERE id::text = $1"
        row = await db.fetchrow(sql, ref_id)
        if row:
            context = json.dumps(row["content_json"], indent=2)
        else:
            # C6 world: many HTML-NNs live only on disk under REPO_ROOT/nodes.
            # DB miss must fall back to that store rather than empty nonsense.
            context = await _browse_node_from_disk(ref_id)
            if context is None:
                context = (
                    f"No detailed context available for {source_type} {ref_id}: "
                    f"not in the nodes table and no matching HTML under "
                    f"REPO_ROOT/nodes/."
                )
    elif source_type == "profile":
        allow_private = params.get("allow_private", False)
        sql = "SELECT config FROM profiles WHERE id = $1"
        row = await db.fetchrow(sql, ref_id)
        if row:
            config = row["config"]
            if isinstance(config, str):
                config = json.loads(config)
            if not allow_private:
                config.pop("pii_vault", None)
            context = json.dumps(config, indent=2)
        else:
            context = f"No detailed context available for {source_type} {ref_id}."
    elif source_type == "web":
        if str(ref_id).startswith("http"):
            try:
                import httpx
                import re
                from lxml import html
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    resp = await client.get(ref_id, headers=headers)
                    if resp.status_code == 200:
                        tree = html.fromstring(resp.content)
                        for bad in tree.xpath("//script | //style | //nav | //footer | //header | //noscript | //iframe"):
                            if bad.getparent() is not None:
                                bad.getparent().remove(bad)
                        body = tree.xpath("//body")
                        if body:
                            text = body[0].text_content()
                            context = re.sub(r'\n\s*\n', '\n\n', text).strip()
                        else:
                            context = re.sub(r'\n\s*\n', '\n\n', tree.text_content()).strip()
                    else:
                        context = f"Failed to fetch {ref_id} (HTTP {resp.status_code})"
            except Exception as e:
                context = f"Error fetching {ref_id}: {e}"
        else:
            context = f"Invalid URL for web result: {ref_id}"
    else:
        context = f"No detailed context available for {source_type} {ref_id}."
    
    # Apply Promptware Defense: redact secrets and scan for injections
    try:
        from sdk.gateway.promptware import redact_and_scan
        safe_context = await redact_and_scan(context)
    except ImportError:
        safe_context = context
        
    return {"context": safe_context}


async def handle_memory_record_completed_turn(params: dict[str, Any]) -> dict[str, Any]:
    """
    Save completed turn to conversation_turns and memory_index.
    """
    session_id = params.get("session_id", "default")
    role = params.get("role", "user")
    content = params.get("content", "")
    tool_calls = params.get("tool_calls", [])
    
    # chat_mode comes from chat_mode.json, NOT from agents.config['chat_mode'] --
    # nothing has ever written that key, so this guard was permanently false and
    # no conversation turn was ever stamped with an emotional state.
    from python.harness.chat_mode import get_current_chat_mode, is_emotional_mode

    chat_mode = get_current_chat_mode()
    agent_id = None
    try:
        active_row = await db.fetchrow("SELECT id FROM agents WHERE is_active = TRUE")
        if active_row:
            agent_id = str(active_row["id"])
    except Exception as exc:
        logger.warning(f"Failed to fetch active agent: {exc}")

    meta = {"title": f"Turn from {role}", "trust_label": "recalled"}
    emotion_state = None
    transcript_content = content

    if is_emotional_mode(chat_mode) and agent_id:
        try:
            from python.harness.mcp_manager import mcp_manager
            emotion_result = await mcp_manager.call_tool("astro-emotion", "get_emotional_state", {"agent_id": agent_id})
            
            if isinstance(emotion_result, dict):
                emotion_state = emotion_result.get("emotional_state") or emotion_result.get("value") or emotion_result
            else:
                emotion_state = emotion_result
                
            if emotion_state:
                meta["emotion"] = emotion_state
                if isinstance(emotion_state, dict):
                    primary = emotion_state.get("primary_emotion", "") or str(emotion_state)
                else:
                    primary = str(emotion_state)
                transcript_content = f"[Emotional State: {primary}]\n{content}"
        except Exception as exc:
            logger.warning(f"Failed to fetch emotional state for RP memory: {exc}")

    # Strip reasoning blocks before indexing.
    #
    # Her <thinking> is scratch work, not knowledge, and indexing it created a
    # self-reinforcing loop: 142 of 860 indexed rows carried reasoning blocks,
    # so a prompt like "run a script" retrieved her own past deliberations and
    # she imitated them -- including the mistakes. Asked to run a self-test she
    # reproduced, verbatim, a clean_broken_skills.py she had been asked to write
    # back on 31 July, because seven of those sessions were sitting in the index
    # as retrievable "memory".
    #
    # The transcript row below keeps the full text for debugging; only the
    # retrievable copy is cleaned.
    indexed_content = re.sub(
        r"<thinking>.*?</thinking>", " ", content, flags=re.DOTALL | re.IGNORECASE
    )
    indexed_content = re.sub(r"</?thinking>", " ", indexed_content, flags=re.IGNORECASE)
    indexed_content = " ".join(indexed_content.split())

    # The emotional prefix goes on the TRANSCRIPT row but not into the memory
    # index.
    #
    # The transcript is the record of what was said and how she felt saying it,
    # so the prefix belongs there. The index is for semantic recall and exact-
    # content dedup, and prefixing it breaks both: the same sentence said at two
    # different moments carries two different prefixes, so LocalMemoryProvider's
    # WHERE NOT EXISTS guard stops matching and the turn indexes twice, while
    # content searches have to match around a prefix that carries no semantic
    # content of its own.
    #
    # This conflict was always latent -- the original code prefixed both -- but
    # it could not surface while the emotion lookup was silently failing and the
    # prefix was never actually produced. The emotional state is still fully
    # recoverable from the index via meta['emotion'].
    await db.execute(
        """
        INSERT INTO conversation_turns (session_id, role, content, tool_calls)
        VALUES ($1, $2, $3, $4::jsonb)
        """,
        session_id, role, transcript_content, json.dumps(tool_calls)
    )

    # Also index it with duplicate control
    manager = get_manager()
    if indexed_content:
        await manager.store(
            source_type="session",
            ref_id=session_id,
            content=indexed_content,
            meta=meta
        )

    learning_payload = _build_learning_node_payload(
        session_id=session_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        params=params,
    )
    learning_result: dict[str, Any] | None = None
    if learning_payload:
        if is_emotional_mode(chat_mode) and emotion_state:
            # We already have tags from the payload, but let's make sure it's a list and append emotion.
            tags = learning_payload.get("tags") or []
            if isinstance(emotion_state, dict):
                primary = emotion_state.get("primary_emotion", "") or "unknown_emotion"
                tags.append(f"emotion:{primary}")
            else:
                tags.append(f"emotion:{emotion_state}")
            learning_payload["tags"] = tags
        
        try:
            learning_result = create_learning_documentation_node(
                topic=learning_payload["topic"],
                summary=learning_payload["summary"],
                canonical_node_id=learning_payload["canonical_node_id"],
                details=learning_payload["details"],
                source_files=learning_payload["source_files"],
                references=learning_payload["references"],
                relationships=learning_payload["relationships"],
                tags=learning_payload["tags"],
                node_id=learning_payload["node_id"],
            )
            learning_path = learning_result.get("path")
            if learning_path:
                await handle_memory_reindex_node({"node_path": learning_path})
        except Exception as exc:
            logger.warning("Failed to create learning node for session %s: %s", session_id, exc)

    # Sync offline-first chat continuity log
    sync_manager.append_session_event(session_id, "completed_turn", {
        "role": role,
        "content": content,
        "tool_calls": tool_calls
    })

    result: dict[str, Any] = {"status": "recorded"}
    if learning_result:
        result["learning_node_path"] = learning_result.get("path", "")
        result["learning_node_id"] = learning_result.get("node_id", "")
    return result


async def handle_memory_scan_context(params: dict[str, Any]) -> dict[str, Any]:
    """
    Scan text for prompt injection patterns.
    """
    text = params.get("text", "")
    try:
        from sdk.gateway.promptware import scan_for_injection
        flagged, safe_text = scan_for_injection(text)
        return {"safe_text": safe_text, "flagged": flagged}
    except ImportError:
        # Fallback if promptware fails to load
        return {"safe_text": text, "flagged": False}

async def handle_memory_reindex_node(params: dict[str, Any]) -> dict[str, Any]:
    """
    Reads an HTML Neural Node from disk, parses it, and stores it in the MemoryManager index.
    """
    node_path = params.get("node_path")
    if not node_path:
        return {"status": "failed", "error": "node_path is required"}
        
    try:
        from sdk.agent.parser import parse_file
        node = parse_file(node_path)
        
        manager = get_manager()
        await manager.store(
            source_type=node.node_type or "node",
            ref_id=node.node_id,
            content=node.semantic_memory,
            meta={
                "title": node.state_data.get("title", f"Node {node.node_id}"),
                "trust_label": node.trust_tier or "verified",
                "relationships": [{"type": r.relationship_type, "target": r.href} for r in node.relationships]
            }
        )
        return {"status": "success", "node_id": node.node_id}
    except Exception as exc:
        logger.exception(f"Failed to reindex node {node_path}")
        return {"status": "failed", "error": str(exc)}


async def handle_research_unified_search(params: dict[str, Any]) -> dict[str, Any]:
    """
    Unified search hitting both local memory and (simulated) web.
    """
    query = params.get("query", "")
    sources = params.get("sources", ["all"])
    
    include_memory = "all" in sources or "memory" in sources or any(s in sources for s in ["session", "node", "task", "workflow", "profile", "nodes", "tasks", "workflows"])
    include_web = "all" in sources or "web" in sources
    
    results = []
    errors = {}
    
    if include_memory:
        mem_sources = [s for s in sources if s in ["session", "node", "task", "workflow", "profile", "nodes", "tasks", "workflows", "memory"]]
        if "all" in sources or "memory" in sources:
            mem_sources = ["session", "task", "node", "workflow", "profile", "documentation", "program-doc", "function-doc"]
            
        try:
            mem_res = await handle_memory_search({"query": query, "sources": mem_sources, "limit": 10})
            results.extend(mem_res.get("results", []))
        except Exception as e:
            logger.warning(f"Unified search memory error: {e}")
            errors["memory"] = str(e)
        
    if include_web:
        try:
            search_provider_id = params.get("search_provider_id")
            # query_detailed rather than query: the renderer already draws a red
            # banner for every entry in `errors`, and the only reason a failing
            # search provider never lit it was that nothing populated the dict
            # unless the whole call threw (UI-020). Non-200s never threw.
            outcome = await SearchManager.query_detailed(
                query, provider_id=search_provider_id
            )
            if outcome.errors:
                errors["web"] = outcome.error_summary()

            # Crawler output is scraped HTML, not a provider's ranked answer, so
            # it is scored below provider results and labelled as such rather
            # than being passed off as the search the user paid for.
            from_crawler = outcome.source == "crawler"
            for res in outcome.results:
                results.append({
                    "id": f"web-{uuid.uuid4().hex[:8]}",
                    "source_type": "web",
                    "title": res.get("title", f"Web Result for: {query}"),
                    "snippet": res.get("snippet", ""),
                    "score": 0.60 if from_crawler else 0.75,
                    "created_at": "",
                    "ref": {"url": res.get("url", "")},
                    "trust": "external",
                    "provider": outcome.provider_name or "",
                    "degraded": outcome.degraded,
                })
        except Exception as e:
            logger.warning(f"Unified search web error: {e}")
            errors["web"] = str(e)
        
    # Sort unified results by score desc
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    
    # Scan context for injection patterns
    for res in results:
        if res.get("snippet"):
            scan_res = await handle_memory_scan_context({"text": res["snippet"]})
            if scan_res.get("flagged"):
                res["snippet"] = scan_res["safe_text"]
                res["flagged"] = True
                
    return {"results": results, "errors": errors}


async def index_profile_chunks(profile_id: str, config_json: dict) -> None:
    """Helper to chunk profile JSON and insert into memory_index"""
    chunks = []
    
    # Extract identity/about
    about_text = ""
    identity = config_json.get("identity", {})
    if identity:
        about_text += f"Identity: {json.dumps(identity, indent=2)}\n"
    about = config_json.get("about", {})
    if about:
        about_text += f"About: {json.dumps(about, indent=2)}\n"
    if about_text:
        chunks.append(("identity_about", "Profile Identity & About", about_text))

    # Extract skills
    skills = config_json.get("skills", [])
    if skills:
        chunks.append(("skills", "Profile Skills", json.dumps(skills, indent=2)))

    # Extract projects
    projects = config_json.get("projects", [])
    if projects:
        chunks.append(("projects", "Profile Projects", json.dumps(projects, indent=2)))

    # Extract preferences
    preferences = config_json.get("preferences", {})
    if preferences:
        chunks.append(("preferences", "Profile Preferences", json.dumps(preferences, indent=2)))

    # Extract relationships/connections
    relationships = config_json.get("relationships", [])
    connections = config_json.get("connections", [])
    rel_text = ""
    if relationships:
        rel_text += f"Relationships: {json.dumps(relationships, indent=2)}\n"
    if connections:
        rel_text += f"Connections: {json.dumps(connections, indent=2)}\n"
    if rel_text:
        chunks.append(("relationships", "Profile Relationships", rel_text))

    # Extract PII-protected fields
    pii = config_json.get("pii_vault", {})
    if pii:
        chunks.append(("pii", "Profile PII Vault (Private)", json.dumps(pii, indent=2)))

    # Fallback to general config if no specific chunks found
    if not chunks and config_json:
        chunks.append(("general", "Profile Data", json.dumps(config_json, indent=2)))
        
    if chunks:
        # Clear existing chunks for this profile
        await db.execute("DELETE FROM memory_index WHERE source_type = 'profile' AND ref_id = $1", profile_id)
        
        # Insert new chunks
        for chunk_type, title, content in chunks:
            trust_label = "private" if chunk_type == "pii" else "profile"
            await db.execute(
                """
                INSERT INTO memory_index (source_type, ref_id, title, content_text, trust_label)
                VALUES ($1, $2, $3, $4, $5)
                """,
                "profile", profile_id, title, content, trust_label
            )

async def handle_memory_reindex_profiles(params: dict[str, Any]) -> dict[str, Any]:
    """
    One-time migration to index existing user profiles into memory_index.
    """
    rows = await db.fetch("SELECT id, config FROM profiles")
    reindexed_count = 0
    for row in rows:
        config = row["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        if config:
            await index_profile_chunks(row["id"], config)
            reindexed_count += 1
            
    return {"status": "success", "profiles_reindexed": reindexed_count}



async def handle_research_distill_to_node(params: dict[str, Any]) -> dict[str, Any]:
    """
    Create a new Neural Node from selected research results.
    """
    selected_results = params.get("results", [])
    if not selected_results:
        content = params.get("content")
        if content:
            selected_results = [{
                "id": f"distill-{uuid.uuid4().hex[:8]}",
                "source_type": params.get("source_type", "unknown"),
                "snippet": content,
                "title": params.get("source_title", "Unknown")
            }]
        else:
            return {"error": "No results provided"}
        
    full_contexts = []
    for r in selected_results:
        # Fetch full context
        ctx_resp = await handle_memory_browse({"source_type": r.get("source_type"), "ref_id": r.get("ref_id", r.get("id"))})
        ctx_text = ctx_resp.get("context", r.get("snippet", ""))
        # Truncate context to avoid overflowing context window
        if len(ctx_text) > 3000:
            ctx_text = ctx_text[:3000] + "... (truncated)"
        full_contexts.append(f"Source [{r.get('title', 'Unknown')}]:\n{ctx_text}")
        
    combined_context = "\n\n---\n\n".join(full_contexts)
    
    from python.inference.llama_server_manager import check_model_cached, generate_stream
    if check_model_cached():
        prompt = f"Please synthesize and summarize the following search results into a cohesive overview. Cite your sources using the source titles provided.\n\n{combined_context}\n\nStart your response with 'Title: ' followed by a short 3-6 word title summarizing the topic, then a blank line, then the summary."
        messages = [{"role": "user", "content": prompt}]
        chunks = []
        async for chunk in generate_stream(messages, max_tokens=1024, temperature=0.5):
            chunks.append(chunk)
        raw_text = "".join(chunks).strip()
        
        node_title = "Distilled Collection"
        summary_text = raw_text
        if raw_text.lower().startswith("title:"):
            parts = raw_text.split("\n", 1)
            node_title = parts[0][6:].strip()
            summary_text = parts[1].strip() if len(parts) > 1 else raw_text
    else:
        node_title = "Distilled Collection"
        summary_text = "Local LLM is not cached. Concatenated sources:\n\n" + "\n\n".join([f"Source ({r['source_type']}): {r.get('title', '')}\n{r.get('snippet', '')}" for r in selected_results])
    
    content_json = {
        "text": summary_text,
        "distilled_from": [r.get("id", "unknown") for r in selected_results]
    }
    
    node_id = await db.fetchval(
        """
        INSERT INTO nodes (node_type, content_json, execution_authority)
        VALUES ('concept', $1::jsonb, 'user')
        RETURNING id
        """,
        json.dumps(content_json)
    )
    
    current_iso_time = datetime.now(timezone.utc).isoformat()
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html_nn = (
        f'<!DOCTYPE html>\n'
        f'<html lang="en">\n'
        f'<head>\n'
        f'  <meta charset="UTF-8">\n'
        f'  <title>{node_title}</title>\n'
        f'</head>\n'
        f'<body>\n'
        f'<article data-node-id="{node_id}" data-node-type="concept" data-schema-version="neural-node/v2" data-execution-authority="1.00" data-memory-authority="1.00">\n'
        f'  <header id="node-identity">\n'
        f'    <h1 id="node-title">{node_title}</h1>\n'
        f'    <dl>\n'
        f'      <dt>Node ID</dt><dd><code>{node_id}</code></dd>\n'
        f'      <dt>Type</dt><dd><code>concept</code></dd>\n'
        f'      <dt>Created</dt><dd><time datetime="{current_iso_time}">{current_date}</time></dd>\n'
        f'    </dl>\n'
        f'  </header>\n'
        f'  <section id="semantic-memory">\n'
        f'    <div class="content">\n      {summary_text}\n    </div>\n'
        f'  </section>\n'
        f'  <script type="application/json" id="node-state" data-cognitive-block="state">\n'
        f'    {json.dumps({"title": node_title, "node_type": "concept"}, indent=2)}\n'
        f'  </script>\n'
        f'</article>\n'
        f'</body>\n'
        f'</html>'
    )
    return {"status": "success", "node_id": str(node_id), "title": node_title, "html_nn": html_nn}

async def handle_memory_save_node(params: dict[str, Any]) -> dict[str, Any]:
    node = params.get("node", {})
    if not node or "id" not in node:
        return {"error": "Missing node data or node ID"}
    
    node_id = node["id"]
    content_json = json.dumps(node)
    
    html_content = node.get("html_nn") or node.get("raw_markdown") or node.get("text", "")
    if html_content and "<article" in html_content:
        try:
            from sdk.agent.parser import parse_html
            # Validation gate: Ensure it parses cleanly under strict v2 schema
            parse_html(html_content)
        except Exception as e:
            return {"error": f"Schema validation failed: {str(e)}"}
    
    import uuid
    try:
        uuid_obj = uuid.UUID(node_id)
    except ValueError:
        # Not a UUID, skip DB sync
        return {"status": "success", "msg": "Node saved to file only (not a UUID)"}
    
    try:
        existing = await db.fetchval("SELECT id FROM nodes WHERE id = $1", str(uuid_obj))
        if existing:
            await db.execute(
                "UPDATE nodes SET content_json = $1::jsonb, node_type = $2, updated_at = NOW() WHERE id = $3",
                content_json, node.get("type", "concept"), str(uuid_obj)
            )
        else:
            await db.execute(
                "INSERT INTO nodes (id, content_json, node_type) VALUES ($1, $2::jsonb, $3)",
                str(uuid_obj), content_json, node.get("type", "concept")
            )
    except Exception as e:
        logger.warning(f"Failed to sync node to DB: {e}")
    else:
        try:
            from sdk.agent.anchor import canonicalize_and_anchor

            state_data = {
                "node_id": str(uuid_obj),
                "node_type": node.get("type", "concept"),
                "content_json": node,
            }
            canonicalize_and_anchor(
                str(uuid_obj),
                html_content or content_json,
                state_data,
                None,
                creator="tala-system",
                document_type="html-nn",
            )
        except Exception as e:
            logger.warning("Failed to anchor node %s on blockchain: %s", node_id, e)
        
    return {"status": "success"}

async def handle_memory_delete_node(params: dict[str, Any]) -> dict[str, Any]:
    node_id = params.get("node_id")
    if not node_id:
        return {"error": "Missing node ID"}
    
    import uuid
    try:
        uuid_obj = uuid.UUID(node_id)
    except ValueError:
        return {"status": "success", "msg": "File deleted (not a UUID)"}
    
    try:
        await db.execute("DELETE FROM nodes WHERE id = $1", str(uuid_obj))
        # Also clean up memory index
        await db.execute("DELETE FROM memory_index WHERE ref_id = $1", str(uuid_obj))
    except Exception as e:
        logger.warning(f"Failed to delete node from DB: {e}")
        
    return {"status": "success"}

async def handle_memory_get_user_model(params: dict[str, Any]) -> dict[str, Any]:
    """
    Retrieve the reconstructed offline-first user memory model.
    """
    state = sync_manager.reconstruct_memory()
    return {"status": "success", "user_model": state}


async def handle_memory_rebuild_from_disk(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Rebuild Chroma + FTS memory indexes from nodes/*.html on disk (UI-audit C6).

    Disk HTML under the pinned repo ``nodes/`` directory is the source of
    truth. Success is honest: Chroma must be readable after index, FTS
    reindex failures fail the call, and an empty disk is reported as an
    explicit empty recovery — not a silent success that looks healthy.
    """
    params = params or {}
    from python.harness.config import settings
    from python.harness import retrieval

    # Pin nodes_dir: never trust a caller-supplied path (attacker-controlled
    # params must not reindex arbitrary trees into memory).
    nodes_dir = (settings.REPO_ROOT / "nodes").resolve()
    allow_empty = bool(params.get("allow_empty", False))
    if not nodes_dir.is_dir():
        return {"status": "failed", "error": f"nodes_dir not found: {nodes_dir}"}

    disk_html = sorted(p for p in nodes_dir.glob("*.html") if "_archive_legacy" not in str(p))
    retrieval._INDEXED_MTIMES.clear()
    try:
        retrieval.index_nodes(nodes_dir)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"chroma index failed: {type(exc).__name__}: {exc}",
            "disk_html": len(disk_html),
            "nodes_dir": str(nodes_dir),
        }

    chroma_count = 0
    try:
        if retrieval.node_collection is None:
            return {
                "status": "failed",
                "error": "chroma node collection is not initialised",
                "disk_html": len(disk_html),
                "nodes_dir": str(nodes_dir),
            }
        chroma_count = int(retrieval.node_collection.count() or 0)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"chroma unreadable after index: {type(exc).__name__}: {exc}",
            "disk_html": len(disk_html),
            "nodes_dir": str(nodes_dir),
        }

    fts_ok = 0
    fts_fail = 0
    force_all = bool(params.get("force_all", True))
    limit = None if force_all else int(params.get("limit") or 50)
    targets = disk_html if limit is None else disk_html[:limit]
    for path in targets:
        try:
            res = await handle_memory_reindex_node({"node_path": str(path)})
            if res.get("status") == "success":
                fts_ok += 1
            else:
                fts_fail += 1
        except Exception:
            fts_fail += 1

    if fts_fail > 0:
        return {
            "status": "failed",
            "error": f"FTS reindex failed for {fts_fail} node(s)",
            "disk_html": len(disk_html),
            "chroma_indexed": chroma_count,
            "fts_reindexed": fts_ok,
            "fts_failed": fts_fail,
            "nodes_dir": str(nodes_dir),
        }

    if not disk_html:
        # Deliberate empty recovery only when the operator opts in.
        status = "empty" if allow_empty else "failed"
        return {
            "status": status,
            "error": None if allow_empty else "nodes/ has no HTML to rebuild; pass allow_empty=true for empty recovery",
            "disk_html": 0,
            "chroma_indexed": chroma_count,
            "fts_reindexed": 0,
            "fts_failed": 0,
            "nodes_dir": str(nodes_dir),
        }

    if chroma_count <= 0:
        return {
            "status": "failed",
            "error": "chroma still empty after indexing disk HTML",
            "disk_html": len(disk_html),
            "chroma_indexed": chroma_count,
            "fts_reindexed": fts_ok,
            "fts_failed": fts_fail,
            "nodes_dir": str(nodes_dir),
        }

    return {
        "status": "success",
        "disk_html": len(disk_html),
        "chroma_indexed": chroma_count,
        "fts_reindexed": fts_ok,
        "fts_failed": fts_fail,
        "nodes_dir": str(nodes_dir),
    }

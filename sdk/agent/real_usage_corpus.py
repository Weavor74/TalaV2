"""
sdk/agent/real_usage_corpus.py

Two corpus-sync steps that feed the LoRA voice adapter from Tala's real
experience instead of synthetic fiction: her own turns from
`conversation_turns` (real chat sessions) and her HTML-NN skills and memories
(the nodes use creates: skills, lessons, remembered facts). scripts/train_voice_adapter.py runs both ahead of
every dry run or training run, fresh each time (full regeneration -- at this
data volume, reprocessing everything is cheap and avoids persisted-cursor
complexity for no benefit).

WHICH TURNS ARE HER VOICE. Measured on the real table (2026-10-24): 1,783
assistant rows, but 1,422 of them belong to uuid-named sessions -- the planner,
coder and goal engine calling ``inference.generate`` with no session id -- and
only 339 to the renderer's ``chat-<timestamp>`` sessions, which are the actual
conversations. Training the voice on agent output would teach task narration,
code and a constant stored emotion label. So only chat sessions are read, her
``<thinking>`` is dropped (the UI shows it; it is not how she sounds), the
emotion prefix a past run stored is replaced by the one the engine derives for
that moment, and turns that are tool narration, code, markup, leaks or
fragments are excluded with a counted reason.

Both sources write into data/training_corpus/, which
scripts/train_voice_adapter.py's extract_turns() already walks recursively
with zero changes needed there -- it just needs `<div class="turn"
data-speaker="tala" data-emotion="...">` elements under an article carrying
`data-era="..."`.

SAFETY: exclude on any signal, never redact-and-include. Training bakes text
into weights permanently -- a turn redacted in place and then trained on is a
worse outcome than that turn simply not existing in the corpus, because
imperfect redaction is silently permanent and a dropped example costs
nothing. See _passes_safety_filter(): two independent checks, both run with a
hardcoded rule set, deliberately NOT GuardrailsManager.load_current() (which
reads Steven's live, currently-disabled config -- using it here would filter
nothing).

Emotion is derived, never authored: scripts/build_story_node.py's
engine_emotion_for() (a live astro-engine lookup from a real timestamp) is
reused as-is for both sources, the same function the synthetic corpus uses.
"""
from __future__ import annotations

import html
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "data" / "training_corpus"
REAL_USAGE_DIR = CORPUS_ROOT / "real-usage"
LEARNING_EVENTS_DIR = CORPUS_ROOT / "learning-events"

MIN_CHARS = 30
MAX_CHARS = 3000

_THINKING_BLOCK = re.compile(r"<thinking>.*?</thinking>", re.S | re.I)
_THINKING_UNCLOSED = re.compile(r"<thinking>.*", re.S | re.I)
_EMOTION_PREFIX = re.compile(r"^\s*\[Emotional State:[^\]]*\]\s*", re.I)
_MARKUP = re.compile(r"<(?:tool|call|search|web|html|div|p)\b|</", re.I)
_BARE_TOOL_CALL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(.*\)$", re.S)
_TOOL_NARRATION = re.compile(r"^\W*\(?Executing\b|\*\(Executing")

#: Hardcoded, deliberately NOT GuardrailsManager.load_current(): the live
#: guardrails config is currently {"category": "none"}, which would filter
#: nothing. Training is permanent and must not depend on an editable live
#: setting. category="custom" makes GuardrailsManager._resolve_rules() fill
#: in the fields this doesn't set (pii_fields, custom_blocklist_terms,
#: toxicity_threshold) with its own safe defaults rather than silently
#: defaulting them to empty/off -- any OTHER category string here would make
#: _resolve_rules() discard this dict entirely and return {}, which would
#: make every check below a silent no-op.
_SAFETY_CATEGORY = "custom"
_SAFETY_RULES = {
    "pii": True,
    "toxicity": True,
    "sql_injection": True,
    "custom_blocklist": True,
    "gibberish": True,
}


def _passes_safety_filter(text: str) -> bool:
    """
    True only if *text* is safe to bake into permanent weights unmodified.

    Two independent checks; either one firing excludes the item entirely.
    Never "use the cleaned version" -- an imperfectly redacted turn that gets
    trained on is a worse outcome than that turn simply not existing.
    """
    if not text or not text.strip():
        return False

    from sdk.gateway.redaction import redact_string

    if redact_string(text) != text:
        return False

    from python.harness.guardrails_manager import GuardrailsManager
    from sdk.agent.exceptions import PiiAccessBlockedError, SafetyGuardrailViolation

    manager = GuardrailsManager(category=_SAFETY_CATEGORY, rules=dict(_SAFETY_RULES), audit=False)  # a probe, not a violation
    try:
        cleaned = manager.validate_output(text)
    except (SafetyGuardrailViolation, PiiAccessBlockedError):
        return False
    return cleaned == text


def _clean_reply(text: str) -> str:
    """Her words without her reasoning and without the emotion label a past run stored with them."""
    text = _THINKING_BLOCK.sub(" ", text or "")
    text = _THINKING_UNCLOSED.sub(" ", text)
    return _EMOTION_PREFIX.sub("", text.strip()).strip()


def _quality_exclusion(original: str, text: str) -> str | None:
    """Why a cleaned reply is not her conversational voice, or None if it is."""
    if not text:
        return "thinking only" if original.strip() else "empty"
    if "```" in text:
        return "code fence"
    if _MARKUP.search(text):
        return "markup"
    if _TOOL_NARRATION.search(text):
        return "tool narration"
    if _BARE_TOOL_CALL.match(text):
        return "bare tool call"
    if "SYSTEM REMINDER" in text.upper():
        return "system reminder"
    if text.startswith("Traceback (most recent call last)"):
        return "traceback"
    if "\ufffd" in text:
        return "replacement char"
    if len(text) < MIN_CHARS:
        return "too short"
    if len(text) > MAX_CHARS:
        return "too long"
    return None


def _render_turn(text: str, emotion: str, tool_calls_text: str = "") -> str:
    parts = [f'  <div class="turn" data-speaker="tala" data-emotion="{html.escape(emotion)}">']
    # The same surface form memory.py writes and the synthetic stories carry.
    parts.append(f"    <p>[Emotional State: {html.escape(emotion)}]\n{html.escape(text)}</p>")
    if tool_calls_text:
        parts.append(f'    <pre class="tool-call">{html.escape(tool_calls_text)}</pre>')
    parts.append("  </div>")
    return "\n".join(parts)


def _render_corpus_document(era: str, turns: list[str]) -> str:
    body = "\n\n".join(turns)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head><meta charset="UTF-8"></head>\n<body>\n'
        f'<article data-era="{html.escape(era)}">\n{body}\n</article>\n'
        "</body>\n</html>\n"
    )


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_after(when: datetime | None, since: datetime | None) -> bool:
    """True if ``when`` is later than ``since``; with no ``since`` everything is new. Naive times are read as UTC."""
    if since is None:
        return True
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return when > since


def _derive_emotion(when: datetime | None) -> str:
    """engine_emotion_for() is the same live astro-engine lookup the
    synthetic corpus already uses -- reused, not reinvented, so emotion
    labels stay on one axis across every corpus source."""
    if when is None:
        return "neutral"
    try:
        from scripts.build_story_node import engine_emotion_for

        return engine_emotion_for(when)
    except Exception:
        logger.warning("real_usage_corpus: emotion derivation failed; using 'neutral'", exc_info=True)
        return "neutral"


# ---------------------------------------------------------------------------
# Source 1: conversation_turns (real chat history)
# ---------------------------------------------------------------------------

async def sync_real_usage_corpus(since: datetime | None = None) -> dict[str, Any]:
    """
    Rewrite data/training_corpus/real-usage/ from Tala's own
    (role='assistant') conversation_turns rows in the renderer's chat sessions.
    Only her own words become training text -- the direct analogue of the
    synthetic corpus's data-speaker="tala" filter, and a meaningful privacy
    narrowing on its own before any content filter even runs. A failed query
    leaves the previous corpus in place. ``new_since`` counts the kept replies written after ``since`` (all of them when it is
    None): what the retraining trigger asks, "how much is new since the adapter was trained".
    """
    from python.harness import db
    from sdk.agent.trajectory import _normalise_tool_calls

    try:
        rows = await db.fetch(
            """
            SELECT content, tool_calls, created_at
            FROM conversation_turns
            WHERE role = 'assistant' AND session_id LIKE 'chat-%'
            ORDER BY created_at ASC
            """
        )
    except Exception as exc:
        logger.warning("real_usage_corpus: could not query conversation_turns: %s", exc)
        return {"success": False, "error": str(exc), "scanned": 0, "excluded": 0, "excluded_by": {},
                "included": 0, "new_since": 0}

    scanned = len(rows)
    excluded_by: dict[str, int] = {}
    new_since = 0
    turns: list[str] = []
    seen: set[str] = set()

    def exclude(reason: str) -> None:
        excluded_by[reason] = excluded_by.get(reason, 0) + 1

    for row in rows:
        original = row["content"] or ""
        text = _clean_reply(original)
        reason = _quality_exclusion(original, text)
        if reason:
            exclude(reason)
            continue
        if not _passes_safety_filter(text):
            exclude("safety filter")
            continue
        if text[:200] in seen:
            exclude("duplicate")
            continue
        seen.add(text[:200])

        tool_calls_text = ""
        try:
            calls = _normalise_tool_calls(row["tool_calls"])
            if calls:
                import json as _json

                tool_calls_text = _json.dumps(calls)
        except Exception:
            tool_calls_text = ""

        emotion = _derive_emotion(row["created_at"])
        turns.append(_render_turn(text, emotion, tool_calls_text))
        if _is_after(row["created_at"], since):
            new_since += 1

    included = len(turns)
    excluded = sum(excluded_by.values())
    _reset_dir(REAL_USAGE_DIR)
    if turns:
        REAL_USAGE_DIR.mkdir(parents=True, exist_ok=True)
        (REAL_USAGE_DIR / "real-usage-corpus.html").write_text(
            _render_corpus_document("real-usage", turns), encoding="utf-8",
        )

    logger.info(
        "real_usage_corpus: conversation_turns scanned=%d excluded=%d included=%d excluded_by=%s",
        scanned, excluded, included, excluded_by,
    )
    return {"success": True, "scanned": scanned, "excluded": excluded, "excluded_by": excluded_by, "included": included,
            "new_since": new_since}


# ---------------------------------------------------------------------------
# Source 2: real HTML-NN learning-event nodes
# ---------------------------------------------------------------------------

def _article_and_semantic_text(path: Path) -> tuple[Any, str] | tuple[None, None]:
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None, None
    articles = doc.xpath("//article")
    if not articles:
        return None, None
    article = articles[0]
    sections = doc.xpath('.//section[@id="semantic-memory"]')
    text = sections[0].text_content().strip() if sections else ""
    return article, text


def _is_experience(article: Any) -> bool:
    """Is this node one of her skills or memories, as opposed to generated documentation, settings, a tool's manual or her
    protected persona/identity? Skills are ``skill`` nodes; memories are what the system wrote down from use: ``learning.*``
    (lessons, run notes) and ``memory.*`` (remembered facts). The dream journal is a log of weight changes, not a memory."""
    if (article.get("data-document-purpose") or "") == "description":
        return False
    target = article.get("data-doc-target") or ""
    if target == "memory.dream-journal":
        return False
    return article.get("data-node-type") == "skill" or target.startswith(("learning.", "memory."))


async def sync_learning_events_corpus(since: datetime | None = None) -> dict[str, Any]:
    """
    Rewrite data/training_corpus/learning-events/ from her skills and memories: the HTML-NN nodes use creates (see
    ``_is_experience``). HTML-NN is where she keeps what she knows, so it is a training source beside her chats. Failure-recovery
    notes ("the step failed, retried three times") are left out: they are incident reports, not something she learned.
    ``new_since`` counts the kept nodes written after ``since`` (all of them when it is None).
    """
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    try:
        nodes_dir = resolve_active_nodes_dir()
        paths = sorted(nodes_dir.glob("*.html")) if nodes_dir.exists() else []
    except Exception as exc:
        logger.warning("real_usage_corpus: could not list the active nodes directory: %s", exc)
        return {"success": False, "error": str(exc), "scanned": 0, "excluded": 0, "excluded_by": {}, "included": 0, "new_since": 0}

    scanned = 0
    excluded_by: dict[str, int] = {}
    new_since = 0
    written: list[str] = []

    def exclude(reason: str) -> None:
        excluded_by[reason] = excluded_by.get(reason, 0) + 1

    for path in paths:
        article, text = _article_and_semantic_text(path)
        if article is None or not _is_experience(article):
            continue
        scanned += 1

        if path.name.startswith("learning-recovery-"):
            exclude("recovery note")
            continue
        if not text:
            exclude("empty")
            continue
        if not _passes_safety_filter(text):
            exclude("safety filter")
            continue

        when = _parse_timestamp(article.get("data-generated-at") or "")
        if when is None:
            try:
                when = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)  # a node with no stamp is as old as its file
            except OSError:
                when = None
        written.append(_render_turn(text, _derive_emotion(when)))
        if _is_after(when, since):
            new_since += 1

    included = len(written)
    excluded = sum(excluded_by.values())
    _reset_dir(LEARNING_EVENTS_DIR)
    if written:
        LEARNING_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        for index, turn in enumerate(written):
            (LEARNING_EVENTS_DIR / f"learning-event-{index:04d}.html").write_text(
                _render_corpus_document("learning-events", [turn]), encoding="utf-8",
            )

    logger.info(
        "real_usage_corpus: skill and memory nodes scanned=%d excluded=%d included=%d excluded_by=%s",
        scanned, excluded, included, excluded_by,
    )
    return {"success": True, "scanned": scanned, "excluded": excluded, "excluded_by": excluded_by, "included": included,
            "new_since": new_since}

"""
What a chat turn carries into the model, and the caps that keep it from carrying more than it needs.

Measured, not assumed: the "~25k tokens" in the harness logs is the token gate's count *before* the tool documentation is
swapped for the provider's native tools (a local model is sent ~5-7k). What actually varied turn to turn, with nothing
bounding it, was one retrieved node (4,500 tokens at the worst), the settings snapshot on every voice turn, and ten turns of
history in front of a reply that is spoken aloud. Those three are bounded here; roleplay memory is deliberately left alone.

The gate itself is not moved. It runs before a provider is chosen, because a remote provider is billed for the XML tool
documentation the local ones never receive; :func:`reconcile` puts the engine's own processed-token count beside it instead.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: ~800 tokens of a retrieved node's semantic memory (cl100k averages ~4 characters a token).
NODE_MEMORY_CHARS = 3200

TEXT_HISTORY_TURNS = 10
TEXT_OLDER_TURN_CHARS = 4000  # older turns past this are cut; the newest is never cut
VOICE_HISTORY_TURNS = 6
VOICE_OLDER_TURN_CHARS = 1500

SETTINGS_NODE_IDS = ("settings-system", "settings-inference", "settings-runtime", "settings-agent")

# A spoken question that is about the machine itself. Word boundaries, no bare fragments (see the substring-gate history);
# a false positive only keeps the snapshot a turn that did not need it, so the list leans toward keeping.
_ABOUT_THE_SYSTEM_RE = re.compile(
    r"\b(?:settings?|providers?|models?|gpu|vram|cpu|ram|memory|inference|runtime|engine|config(?:uration|ured)?|"
    r"version|context (?:size|window|length)|firewall|guardrails?|running on|are you (?:on|using|running))\b",
    re.IGNORECASE,
)


def is_voice_turn(params: dict[str, Any]) -> bool:
    """A turn the renderer started from the microphone carries a ``voice_turn_id``."""
    return bool(params.get("voice_turn_id"))


def cap_memory(text: str) -> str:
    """Bound one retrieved node's text, cutting on a line or word boundary and saying so."""
    if len(text) <= NODE_MEMORY_CHARS:
        return text
    cut = text[:NODE_MEMORY_CHARS]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > NODE_MEMORY_CHARS // 2:
        cut = cut[:boundary]
    return f"{cut.rstrip()}\n[... node text truncated: {len(text) - len(cut)} more characters not shown ...]"


def settings_node_ids(user_text: str, voice: bool) -> tuple[str, ...]:
    """Which settings nodes make up the live snapshot. A text turn always gets them all; a voice turn gets them only
    when what was said is about the system, since a spoken answer rarely needs 400 tokens of configuration."""
    if not voice or _ABOUT_THE_SYSTEM_RE.search(user_text or ""):
        return SETTINGS_NODE_IDS
    return ()


def history_messages(history: Any, voice: bool) -> list[dict[str, str]]:
    """The conversation so far as chat messages: the last N turns, older oversized ones cut, the newest kept whole."""
    if not isinstance(history, list):
        return []
    turns = TEXT_HISTORY_TURNS if not voice else VOICE_HISTORY_TURNS
    older_cap = TEXT_OLDER_TURN_CHARS if not voice else VOICE_OLDER_TURN_CHARS
    recent = history[-turns:]
    out: list[dict[str, str]] = []
    for idx, msg in enumerate(recent):
        role = "assistant" if msg.get("role") in ("agent", "assistant") else "user"
        content = (msg.get("content") or "").strip()
        if len(content) > older_cap and idx < len(recent) - 1:
            content = content[:older_cap] + "\n... [truncated for context limit preservation] ..."
        if content:
            out.append({"role": role, "content": content})
    return out


def reconcile(gate_tokens: int, timing: dict[str, Any] | None, session_id: str) -> dict[str, Any] | None:
    """Attach the gate's pre-routing estimate to the engine's own count and log both, so "25k" stops being read as what
    the model was sent. Returns the timing payload unchanged when the provider reported none."""
    if timing is None:
        return None
    timing = {**timing, "gate_estimate_tokens": gate_tokens}
    logger.info("prompt tokens  session=%s  gate estimate (pre-routing)=%s  engine processed=%s",
                session_id, gate_tokens, timing.get("prompt_tokens"))
    return timing

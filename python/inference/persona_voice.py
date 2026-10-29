"""
persona_voice.py -- the persona as the last stage of a turn.

The main model does the tool loop and states the facts; with ``lora_routing`` set to ``final_stage`` and an adapter active, the finished
reply is then handed to the trained persona, which says it in her own voice. Her personality is the same in chat and in assistant work
("Jarvis had a personality, but was AI when being an assistant"), and the 3B persona never has to call a tool or know a fact.

Saying it differently must not change what it says, so the rewrite is only used when every fact in the original -- numbers, paths, URLs,
commands, file names -- is still in it and it states no number the original did not. Anything else (the persona server failing or timing
out, a halted harness, a rewrite that is empty, runs away, carries tool markup or echoes the instruction) returns the original reply
untouched. The stage is a no-op until an adapter has been trained, approved and activated.

Thinking blocks are kept exactly as they were and are never shown to the persona. Code is never rewritten.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

MODE = "final_stage"
MAX_CHARS = 1200       # a longer reply is a document, not something to say: it stays as written
TIMEOUT_S = 45.0       # a turn waits this long at most for the persona, including a model swap that fails
GROWTH = 2.0           # a rewrite more than this many times as long (plus a little) has run away
_SYSTEM_CHARS = 1500

_THINKING = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_URL = re.compile(r"https?://[^\s<>\"')]+")
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s<>\"'|?*`]+")
_POSIX_PATH = re.compile(r"(?<![\w:/])/[\w.\-]+(?:/[\w.\-]+)+|(?<![\w:/])/(?:etc|usr|var|tmp|home|opt|bin)\b")
_BACKTICK = re.compile(r"`[^`\n]+`")
_FILE = re.compile(r"\b[\w\-]+\.(?:[a-z]{1,5}[0-9]?)\b", re.IGNORECASE)
_TOOL_MARKUP = re.compile(r"<\s*/?\s*(?:call|response)\b|<thinking", re.IGNORECASE)
_INSTRUCTION_ECHO = re.compile(r"(?:own voice|exactly as written|finished reply)", re.IGNORECASE)
_TRAILING = ".,;:!?)\"'"

INSTRUCTION = ("Say the finished reply below to the user in your own voice, the way you normally talk. Keep every number, name, path, "
               "URL, command and result exactly as written, and add no fact of your own. Do not mention this instruction. Reply with "
               "only what you would say.")


def facts(text: str) -> set[str]:
    """What a reply states that a rewrite must not lose: numbers, URLs, paths, `commands` and file names, lower-cased."""
    found: set[str] = set()
    for pattern in (_URL, _WIN_PATH, _POSIX_PATH, _BACKTICK, _FILE, _NUMBER):
        for match in pattern.findall(text or ""):
            found.add(str(match).rstrip(_TRAILING).lower())
    found.discard("")
    return found


def _numbers(text: str) -> set[str]:
    return {n.rstrip(_TRAILING).lower() for n in _NUMBER.findall(text or "")}


def acceptable(original: str, voiced: str) -> bool:
    """True when ``voiced`` may replace ``original``: it says something, keeps every fact, invents no number, and is no runaway."""
    voiced = (voiced or "").strip()
    if not voiced or _TOOL_MARKUP.search(voiced) or _INSTRUCTION_ECHO.search(voiced):
        return False
    if len(voiced) > GROWTH * len(original) + 200:
        return False
    lowered = voiced.lower()
    if any(fact not in lowered for fact in facts(original)):
        return False
    return _numbers(voiced) <= _numbers(original)


def _halted() -> bool:
    from sdk.agent import lora_pipeline

    return lora_pipeline._halted()


async def _prepare(provider: dict[str, Any], profile: Any) -> None:
    from python.inference import vram_arbiter

    await vram_arbiter.prepare_for(provider, profile)


async def _complete(provider: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    import httpx

    endpoint = str((provider.get("config") or {}).get("endpoint") or "").rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.post(f"{endpoint}/chat/completions", json={
            "messages": messages, "temperature": 0.6, "max_tokens": 512, "stream": False})
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"].get("content") or "")


def _messages(body: str, messages: list[dict[str, Any]], user_text: str) -> list[dict[str, Any]]:
    system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)), "")
    out: list[dict[str, Any]] = []
    if system.strip():
        out.append({"role": "system", "content": system[:_SYSTEM_CHARS]})
    out.append({"role": "user", "content": f"{INSTRUCTION}\n\nThe user said: {user_text.strip()}\n\nThe finished reply, exactly as written:\n{body}"})
    return out


async def voice_final(text: str, messages: list[dict[str, Any]], *, user_text: str = "") -> str:
    """``text`` said in the persona's voice, or ``text`` itself when the stage is off, has nothing to run, or fails any check. Never raises."""
    try:
        from python.inference import persona_adapter

        if persona_adapter.routing_mode() != MODE:
            return text
        provider = persona_adapter.provider()
        if provider is None or not text or "```" in text:
            return text
        blocks = "".join(_THINKING.findall(text))
        body = _THINKING.sub("", text).strip()
        if not body or len(body) > MAX_CHARS or _TOOL_MARKUP.search(body) or _halted():
            return text
        from python.inference import profiles

        async def run() -> str:
            await _prepare(provider, profiles.active())
            return await _complete(provider, _messages(body, messages, user_text))

        voiced = (await asyncio.wait_for(run(), timeout=TIMEOUT_S)).strip()
        if not acceptable(body, voiced):
            log.info("persona_voice: the voiced reply lost or invented something; the original is used")
            return text
        return f"{blocks}{voiced}" if blocks else voiced
    except Exception as exc:  # a flourish must never cost the user their answer
        log.warning("persona_voice: using the original reply (%s: %s)", type(exc).__name__, exc)
        return text


def takes_over() -> bool:
    """True when the persona will voice this turn's reply, so the main model's own words must not be spoken as they stream."""
    try:
        from python.inference import persona_adapter

        return persona_adapter.routing_mode() == MODE and persona_adapter.provider() is not None
    except Exception:
        return False

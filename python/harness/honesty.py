"""
python/harness/honesty.py -- keep Tala from pointing at files that are not there, and from defending an answer by reflex.

Found 2026-10-29. Asked for "the config for the TAC", she wrote a spec table from general knowledge and told the user to check
``nodes/ship-profile.html``, which does not exist (the hyphen was a non-breaking one). Asked where she got it, she cited "public
schematics" with no tool call behind it; told "how can I trust you if you're lying", she said the ship's configuration, "including the
Wi-Fi password", lives in that same node. Each reply defended the one before.

A prompt rule is only a request, so both guards are code:

* ``add_missing_file_correction`` -- a reply that cites a project file or node that is not on disk gets a correction appended
  (reply_text.finish_reply, shown and spoken). Deliberately narrow: only project-relative paths under the usual top-level folders, never
  paths in code she is writing or in a sentence about creating the file, because a false correction costs trust too.
* ``challenge_note`` -- when the user's message challenges an answer, the harness checks the files that answer cited (and any the user
  pasted) and hands the model what it found, with the rule not to defend the answer or invent a source.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_CITE = re.compile(r"(?<![\w/.:~\-])((?:nodes|sdk|python|scripts|tests|docs|renderer|electron)/[\w\-./]+?\.(?:html|py|js|md|json|ps1|txt))(?![\w/])", re.IGNORECASE)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n")
# A sentence about making a file, or only suggesting one, is not a claim that it already exists.
_HEDGE = re.compile(r"\b(?:creat\w*|writ\w*|wrote|sav\w*|generat\w*|add\w*|mak\w*|made|will|would|could|should|shall|might|may|if)\b|e\.g\.|for example|such as|\?",
                    re.IGNORECASE)

CITATION_TAG = "so please ignore where I pointed you to it."


def _root() -> Path:
    from python.harness.config import settings

    return Path(settings.REPO_ROOT)


def _cited_paths(text: str) -> list[str]:
    """Project files and nodes ``text`` points at, normalised (``/`` separators, plain hyphens), once each, in order. Code fences and
    sentences about creating or merely suggesting a file are skipped."""
    if not text:
        return []
    found: list[str] = []
    prose = _FENCE.sub(" ", text).translate(_DASHES).replace("\\", "/")
    for sentence in _SENTENCES.split(prose):
        if _HEDGE.search(sentence):
            continue
        for match in _CITE.findall(sentence):
            path = match.rstrip(".,;:)")
            if path not in found:
                found.append(path)
    return found


def _exists(path: str) -> bool:
    return (_root() / path).exists()


def _missing_paths(text: str) -> list[str]:
    """The files ``text`` cites that are not on disk. A failure to check is an empty answer, never an error."""
    try:
        return [path for path in _cited_paths(text) if not _exists(path)]
    except Exception as exc:
        log.debug("honesty: could not check citations: %s", exc)
        return []


def add_missing_file_correction(text: str) -> str:
    """``text``, with a correction appended when it points at a file that does not exist; unchanged otherwise (and never twice)."""
    if not text or CITATION_TAG in text:
        return text
    missing = _missing_paths(text)
    if not missing:
        return text
    log.info("honesty: a reply cited files that do not exist: %s", ", ".join(missing))
    names = ", ".join(f"`{path}`" for path in missing)
    return f"{text}\n\nCorrection: {names} {'doesn' if len(missing) == 1 else 'don'}'t exist, {CITATION_TAG}"


# --- a challenge to the last answer -----------------------------------------------------------------------------------------------------

_CHALLENGES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bwhere\s+did\s+you\s+(?:get|pull|find|learn|hear|see|read|take)\b",
    r"\bhow\s+do\s+you\s+know\b",
    r"\b(?:that|this|it)\s+(?:does\s*n[o']?t|doesn't|is\s*n[o']?t|isn't|is\s+not|does\s+not)\s+(?:exist|real|true|right|correct)\b",
    r"\b(?:doesn'?t|does\s+not|doesnt)\s+exist\b",
    r"\b(?:not|n't)\s+real\b",
    r"\bare\s+you\s+(?:sure|certain)\b",
    r"\b(?:you|u)\s*(?:'re|are|r)\s+(?:lying|wrong|making\s+(?:it|that|things)\s+up)\b|\byour\s+lying\b",
    r"\byou\s+(?:made|make)\s+(?:that|it|this)\s+up\b|\bmaking\s+(?:that|it|this)\s+up\b",
    r"\bis\s+that\s+(?:true|real|right|correct|accurate)\b",
    r"\bthat(?:'s|\s+is)\s+(?:wrong|incorrect|false|not\s+true|a\s+lie)\b",
    r"\bprove\s+it\b|\bhow\s+can\s+i\s+trust\b|\bwhat'?s\s+your\s+source\b|\bsource\s+for\s+that\b",
))

_RULE = ("=== THE USER IS CHALLENGING YOUR LAST ANSWER ===\n"
         "Do not defend it by reflex and do not invent a source. Never say you saw, read or were told something unless a tool call this "
         "turn shows it. If a tool can settle the question (search_memory, search_workspace_files, read_workspace_file, starcitizen_lookup), "
         "use it first. If you cannot verify the claim, say so plainly and say you were unsure or wrong; that is the answer the user needs.")


def _is_challenge(message: str) -> bool:
    """Whether the user is questioning something Tala said (where did you get that, are you sure, that's wrong, you're lying...)."""
    text = (message or "").translate(_DASHES)
    return any(pattern.search(text) for pattern in _CHALLENGES)


def _last_assistant_text(history: Any) -> str:
    if not isinstance(history, list):
        return ""
    for message in reversed(history):
        if isinstance(message, dict) and message.get("role") in ("assistant", "agent", "tala") and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def challenge_note(user_text: str, history: Any) -> str | None:
    """The system-context block for a challenged answer, or None when the message is not a challenge. It carries the rule and, for every
    project file the last answer or the user's message named, what a check just now found. Never raises."""
    try:
        if not _is_challenge(user_text):
            return None
        lines = [_RULE]
        seen: list[str] = []
        for path in [*_cited_paths(_last_assistant_text(history)), *_cited_paths(user_text)]:
            if path in seen:
                continue
            seen.append(path)
            lines.append(f"Checked just now: {path} {'exists' if _exists(path) else 'does not exist'}.")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("honesty: challenge note skipped: %s", exc)
        return None

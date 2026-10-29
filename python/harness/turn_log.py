"""
python/harness/turn_log.py -- what happened in one chat turn, kept so the reply can be checked against it before it is shown.

Found 2026-10-29. Asked for the Starlancer TAC's default components, Tala wrote a sixteen-row table from nothing. Challenged ("where did you
get that"), she ran starcitizen_lookup for real and then said the spec came from the API -- which had returned dimensions, mass, cargo, crew
and emissions, and no components. She also called a link she had built herself "the link that the lookup returned".

The log records every tool call and its result, and the answer the user challenged. On a challenged turn in which a tool succeeded, the
specifics of that answer (quoted names, measured quantities, big numbers) are looked for in what the tools returned, and the ones that are not
there are named in a correction. A link in the reply that no result contains is called out the same way. Like the other guards it is code:
a prompt rule is only a request. It is deliberately narrow (challenged turns only, a floor of three unsupported details, no correction when
the reply already withdraws the answer) because a false correction costs trust too.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from python.harness import honesty

log = logging.getLogger(__name__)

_QUOTED = re.compile(r"[“\"]([^”\"\n]{3,40})[”\"]")
_MEASURED = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(mm|cm|km|m/s|scm|mw|kw|gw|kg|tonnes?|tons?|m|%)(?![\w])")
_BIG_NUMBER = re.compile(r"(?<![\w.,])(\d{3,})(?![\w])")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_URL = re.compile(r"https?://[^\s)\]>\"'<]+")
# The reply already withdraws or qualifies the answer, so a correction would only repeat it.
_WITHDRAWS = re.compile(r"\bmade\s+(?:\w+\s+){0,3}up\b|\binvent\w*|\bguess(?:ed)?\b|\bfrom\s+memory\b|\bgeneral\s+knowledge\b|\bunverified\b|\bnot\s+verified\b|"
                        r"\bno\s+source\b|\b(?:was|were|is|are)n'?t\s+(?:from|in)\b|\bnot\s+(?:from|in)\s+(?:the|that|this)\s+(?:lookup|result|record|data|api)\b",
                        re.IGNORECASE)

UNSUPPORTED_TAG = "so they did not come from it"
LINK_TAG = "I built it myself rather than being given it"
MIN_UNSUPPORTED = 3
MAX_LISTED = 8


def _specifics(text: str) -> list[str]:
    """What an answer asserts that a source could confirm or not, lower-cased and de-duplicated, in order: quoted names, quantities with a
    unit (``25 mm``), and numbers of three digits or more."""
    plain = honesty.plain(text or "")
    found: list[str] = [match.strip() for match in _QUOTED.findall(plain)]
    found += [f"{number} {unit}" for number, unit in _MEASURED.findall(plain)]
    found += _BIG_NUMBER.findall(_MEASURED.sub(" ", plain))
    return list(dict.fromkeys(found))


def _numbers_in(haystack: str) -> set[str]:
    return set(_NUMBER.findall(re.sub(r"(?<=\d),(?=\d)", "", haystack)))


def _supported(item: str, haystack: str, numbers: set[str]) -> bool:
    if item[0].isdigit():
        return item.split()[0].replace(",", "") in numbers
    return item in haystack


class TurnLog:
    """Every tool call this turn (name, failure reason or None, result) and the answer the user challenged, if they challenged one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, Any]] = []
        self.challenged = ""

    @property
    def outcomes(self) -> list[tuple[str, str | None]]:
        return [(name, reason) for name, reason, _ in self.calls]

    def record(self, tool: str, result: Any) -> None:
        self.calls.append((tool, honesty.tool_failure(result), result))

    def challenge_note(self, user_text: str, history: Any) -> str | None:
        """honesty.challenge_note, remembering which answer was challenged so the reply can be checked against what the tools return."""
        note = honesty.challenge_note(user_text, history)
        if note:
            self.challenged = honesty.last_assistant_text(history)
        return note

    def _haystack(self) -> str:
        results = [json.dumps(result, default=str, ensure_ascii=False) for _, reason, result in self.calls if reason is None]
        return honesty.plain(" ".join(results))

    def add_unsupported_note(self, text: str) -> str:
        """``text``, with a correction appended naming what the challenged answer said that no tool result supports; unchanged otherwise."""
        if not text or not self.challenged or UNSUPPORTED_TAG in text or LINK_TAG in text or _WITHDRAWS.search(text):
            return text
        try:
            haystack = self._haystack()
            if not haystack.strip():
                return text
            numbers = _numbers_in(haystack)
            specifics = _specifics(self.challenged)
            missing = [item for item in specifics if not _supported(item, haystack, numbers)]
            if len(missing) < MIN_UNSUPPORTED or len(missing) * 2 < len(specifics):
                missing = []
            links = [url for url in dict.fromkeys(re.sub(r"^https?://", "", u).rstrip(".,;:/") for u in _URL.findall(honesty.plain(text))) if url not in haystack]
            sentences = []
            if missing:
                shown = ", ".join(missing[:MAX_LISTED]) + (f" and {len(missing) - MAX_LISTED} more" if len(missing) > MAX_LISTED else "")
                sentences.append(f"these details from my earlier answer are not in the lookup result, {UNSUPPORTED_TAG}: {shown}; treat them as unverified.")
            if links:
                sentences.append(f"{'the' if not missing else 'The'} link {', '.join(links)} is not in the lookup result{'' if not missing else ' either'}; {LINK_TAG}.")
            if not sentences:
                return text
            log.info("turn_log: a challenged reply rested on details no tool returned (%d details, %d links)", len(missing), len(links))
            return f"{text}\n\nCorrection: {' '.join(sentences)}"
        except Exception as exc:
            log.debug("turn_log: could not check the reply against the tool results: %s", exc)
            return text

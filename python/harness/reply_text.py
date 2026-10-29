"""
python/harness/reply_text.py -- what the user sees, and in a voice turn hears, of a model's reply.

A reply ended with ``<call><code><call name="update_user_model" focus="memory" max_turns="3"/></code></call>``: the model writing
a tool call in a shape the harness does not parse, so it was neither run nor removed, and it sat in the chat bubble and was read
aloud. Markup like that is a protocol, not speech. This removes it, and only it: tags inside backticks or a code fence are left
alone, because she may be explaining the syntax.
"""
from __future__ import annotations

import logging
import re

from python.harness import honesty, turn_log

log = logging.getLogger(__name__)

_PROTECTED = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_SELF_CLOSING = re.compile(r"<call\b[^>]*?/>", re.IGNORECASE)
_PAIRED = re.compile(r"<call\b[^>]*>.*?</call(?::[\w.]+)?>", re.IGNORECASE | re.DOTALL)
_LONE_TAG = re.compile(r"</?call\b[^>]*>", re.IGNORECASE)
_EMPTY_CODE = re.compile(r"<code>\s*</code>", re.IGNORECASE)


def _strip_segment(segment: str) -> str:
    found = False
    for pattern in (_SELF_CLOSING, _PAIRED, _LONE_TAG, _EMPTY_CODE):
        segment, n = pattern.subn("", segment)
        found = found or bool(n)
    if not found:
        return segment
    segment = re.sub(r"[ \t]{2,}", " ", segment)           # the gap a removed tag leaves
    return re.sub(r"[ \t]+\n", "\n", segment)


# The harness's own instructions to the model are written as "[SYSTEM REMINDER]: ..." lines. A small model handed them can read them
# back as its answer (granite3.2-vision:2b did, three times over, and it was spoken aloud). They contain no "[" after the marker,
# so a reminder runs to the next "[" or to the end, whether or not the echo was cut off by the token limit.
_ECHOED_REMINDER = re.compile(r"\[SYSTEM REMINDER\]:[^\[]*", re.IGNORECASE)


def _strip_echoed_reminders(text: str) -> str:
    """``text`` without any ``[SYSTEM REMINDER]: ...`` the model read back; unchanged if there is none. Code spans are left alone."""
    if "system reminder" not in text.lower():
        return text
    parts = _PROTECTED.split(text)
    cleaned = [part if index % 2 else _ECHOED_REMINDER.sub("", part) for index, part in enumerate(parts)]
    result = "".join(cleaned)
    if result == text:
        return text
    log.info("reply_text: removed an echoed system reminder from a reply")
    return re.sub(r"[ 	]{2,}", " ", result).strip()


def _strip_stray_calls(text: str) -> str:
    """``text`` without tool-call markup that was never parsed, and without echoed system reminders; unchanged if there is none."""
    text = _strip_echoed_reminders(text)
    if "<call" not in text.lower() and "</call" not in text.lower():
        return text
    parts = _PROTECTED.split(text)
    cleaned = [part if index % 2 else _strip_segment(part) for index, part in enumerate(parts)]
    result = "".join(cleaned)
    if result == text:
        return text
    log.info("reply_text: removed tool-call markup that was not parsed from a reply")
    return result.strip()


# --- a claim of an action that no tool took ---------------------------------------------------------------------------
#
# "Open a web browser" was answered "I've opened it" in a turn where no tool ran: the model had nothing to open one with, so it described
# doing it. The harness knows whether a tool ran this request; the model's words are not evidence either way. When a reply claims one
# of the usual physical or system actions and nothing ran, the claim is corrected -- in the reply, and aloud (see voice_runtime.turn_done).
# Deliberately narrow (a verb and an object it could only do with a tool): a false correction costs trust too.

CLAIM_CORRECTION = "Correction: I didn't actually do that, because no tool ran this turn."

_OBJECTS = (r"(?:browser|window|tab|web\s*site|site|web\s*page|page|url|link|app|application|program|terminal|command|script|file|folder"
            r"|e-?mail|message|download)s?")
_DONE = (r"(?:opened|launched|started|loaded|pulled\s+up|brought\s+up|fired\s+up|clicked|typed|navigated|ran|executed|closed|created"
         r"|deleted|saved|sent|installed|downloaded)")
_CLAIMS = (
    # "I opened the browser", "I've just launched the app" -- not "I opened with a joke", "I started by saying"
    re.compile(r"\bI(?:'ve|\s+have)?(?:\s+just)?\s+" + _DONE + r"(?!\s+(?:by|with|to|off|out|from|over|up\s+with)\b)\s+[^.!?\n]{0,40}?\b" + _OBJECTS + r"\b",
               re.IGNORECASE),
    # "Opening your browser now."
    re.compile(r"\b(?:opening|launching|starting|loading|pulling\s+up|bringing\s+up|firing\s+up)\s+(?:up\s+)?(?:your\s+|the\s+|a\s+|an\s+|that\s+|this\s+)?"
               r"(?:(?:new|web|default)\s+)*" + _OBJECTS + r"\b", re.IGNORECASE),
    # "Done, the browser is open."
    re.compile(r"\b(?:browser|window|tab|web\s*site|page|app|application|terminal)\s+(?:is|'s)\s+(?:now\s+|already\s+)?(?:open|opened|running|launched|up)\b",
               re.IGNORECASE),
)
_HEDGE = re.compile(r"\b(?:if|whether|unless|would|could|should|can\s+you|do\s+you\s+want|want\s+me\s+to|shall\s+i)\b|\?", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


def _claims_an_action(text: str) -> bool:
    for index, part in enumerate(_PROTECTED.split(text)):
        if index % 2:  # code spans and fences: she may be showing syntax, not claiming
            continue
        for sentence in _SENTENCE.findall(part):
            if _HEDGE.search(sentence):
                continue
            if any(pattern.search(sentence) for pattern in _CLAIMS):
                return True
    return False


# The model's own chat-template tokens. One in a reply means it stopped answering and began writing the next turns itself (a reply seen
# 2026-10-29 went "...|im_end|> As a highly advanced AI ... |im_start|>assistant" and invented both sides), so the reply ends there.
_TEMPLATE_TOKEN = re.compile(r"<?\|(?:im_start|im_end|endoftext|eot_id|end_of_text|start_header_id|end_header_id)\|>?")


def _cut_at_template_token(text: str) -> str:
    """``text`` up to its first chat-template token; unchanged if there is none. Code spans are left alone (she may be naming the token)."""
    if "|" not in text:
        return text
    offset = 0
    for index, part in enumerate(_PROTECTED.split(text)):
        match = None if index % 2 else _TEMPLATE_TOKEN.search(part)
        if match:
            log.info("reply_text: a reply ran on past a chat-template token; cut there")
            return text[:offset + match.start()].rstrip()
        offset += len(part)
    return text


def finish_reply(text: str, tools_ran: bool, tool_calls: turn_log.TurnLog | list[tuple[str, str | None]] | None = None) -> str:
    """The reply as it should be shown and spoken: markup, echoed reminders and runaway template turns removed, and a claim no tool backs,
    a file that is not there, an answer built on a failed tool call, or details a challenged answer took from no tool result, corrected.
    ``tool_calls`` is the turn's TurnLog, or just its (tool, failure reason) outcomes."""
    log_ = tool_calls if isinstance(tool_calls, turn_log.TurnLog) else None
    text = honesty.add_failed_tool_note(honesty.add_missing_file_correction(_strip_stray_calls(_cut_at_template_token(text))),
                                        log_.outcomes if log_ else tool_calls)
    text = log_.add_unsupported_note(text) if log_ else text
    if tools_ran or not text or CLAIM_CORRECTION in text or not _claims_an_action(text):
        return text
    log.info("reply_text: a reply claimed an action although no tool ran; correction appended")
    return f"{text}\n\n{CLAIM_CORRECTION}"


def trailing_correction(text: str) -> str | None:
    """The corrections finish_reply appended (from the first ``Correction:`` paragraph to the end), or None. A streamed reply is spoken
    before they exist, so voice_runtime.turn_done speaks them afterwards."""
    index = (text or "").find("\n\nCorrection: ")
    return text[index + 2:] if index >= 0 else None

"""
python/harness/voice_chunker.py -- turn a streamed reply into sentences to speak, as soon as each is complete.

The model's text arrives in arbitrary fragments. :class:`SentenceChunker` removes what must never be spoken (the model's
``<thinking>`` blocks, tool-call tags in any shape, ``<code>`` blocks, fenced code), reads markdown as plain words, and emits a sentence the
moment it ends, so the first one can be synthesised and played while the rest is still being generated.

Rules worth knowing:

* a "." followed by a digit ("5.30"), or ending a known abbreviation ("Dr.", "p.m.", "e.g."), is not a sentence end;
* a newline ends a sentence, so list items and headings are spoken one at a time;
* the first sentence is emitted however short ("Sure."): that is the time-to-first-audio. After that, fragments under
  ``MIN_CHARS`` join the next sentence, so speech is not a string of one-word clips;
* a run with no punctuation at all is cut at a comma or space once it passes ``max_chars``, so it still starts.
"""
from __future__ import annotations

import re

MIN_CHARS = 8
MAX_CHARS = 220
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "a.m", "p.m", "no", "approx", "inc",
})
_TAGS = (
    ("<thinking>", "</thinking>"),
    ("<code>", "</code>"),
    ("```", "```"),
)
# A tool call in any of the shapes a model writes it: `<call:tool a="b"/>`, `<call name="x"/>`, `<call:tool>body</call:tool>`, or
# `<call><code><call name="x"/></code></call>`. A self-closing tag is dropped alone; an opening tag hides everything up to its close.
_CALL_OPEN = "<call"
_CALL_CLOSE = re.compile(r"</call(?::[\w.]+)?>")
_OPENERS = tuple(o for o, _ in _TAGS) + (_CALL_OPEN,)
_ANY_TAG = re.compile(r"</?[A-Za-z][^<>]*>")          # whatever tag is left: spoken text never contains one
_OPEN_TAG_TAIL = re.compile(r"<[/A-Za-z][^<>]*$")      # the start of a tag whose end has not arrived yet; "3 < 5" is not one
_SKIP_TAIL = 48                                        # enough of a hidden block's end to still recognise a closer split across fragments
_BOUNDARY = re.compile(r"([.!?]+[\"')\]*_]*)(\s|$)|\n")


def _held_prefix_len(text: str) -> int:
    """How many trailing characters could be the start of an opening tag that has not fully arrived."""
    keep = 0
    for opener in _OPENERS:
        for n in range(1, len(opener)):
            if text.endswith(opener[:n]):
                keep = max(keep, n)
    return keep


def _clean(sentence: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", sentence)          # [text](url) -> text
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"^\s*(?:#+\s+|[-*•]\s+)", "", s)               # heading / bullet marker
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", s)
    return re.sub(r"\s+", " ", s).strip()


class SentenceChunker:
    def __init__(self, max_chars: int = MAX_CHARS, min_chars: int = MIN_CHARS):
        self.max_chars = max_chars
        self.min_chars = min_chars
        self._raw = ""
        self._skip: re.Pattern[str] | None = None
        self._visible = ""
        self._carry = ""
        self._emitted_any = False

    # --- stage 1: remove what must not be spoken ----------------------------------------------------------------

    def _filter(self) -> None:
        while self._raw:
            if self._skip is not None:
                found = self._skip.search(self._raw)
                if not found:
                    self._raw = self._raw[-_SKIP_TAIL:]
                    return
                self._raw = self._raw[found.end():]
                self._skip = None
                continue
            hits = [(self._raw.find(op), op) for op in _OPENERS if self._raw.find(op) >= 0]
            if hits:
                at, opener = min(hits)
                self._visible += _ANY_TAG.sub("", self._raw[:at])
                if opener == _CALL_OPEN:
                    end = self._raw.find(">", at)
                    if end < 0:  # the tag has not finished arriving; keep it whole until it has
                        self._raw = self._raw[at:]
                        return
                    tag = self._raw[at:end + 1]
                    self._raw = self._raw[end + 1:]
                    if not tag.endswith("/>"):
                        self._skip = _CALL_CLOSE
                    continue
                closer = dict(_TAGS)[opener]
                self._raw = self._raw[at + len(opener):]
                self._skip = re.compile(re.escape(closer))
                continue
            keep = _held_prefix_len(self._raw)
            partial = _OPEN_TAG_TAIL.search(self._raw)
            if partial:
                keep = max(keep, len(self._raw) - partial.start())
            speakable = self._raw[:len(self._raw) - keep] if keep else self._raw
            self._visible += _ANY_TAG.sub("", speakable)
            self._raw = self._raw[len(self._raw) - keep:] if keep else ""
            return

    # --- stage 2: cut sentences ----------------------------------------------------------------------------------

    def _is_abbreviation(self, upto: int) -> bool:
        word = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*)$", self._visible[:upto].rstrip(".!?"))
        return bool(word and word.group(1).lower() in _ABBREVIATIONS)

    def _is_list_number(self, period_end: int) -> bool:
        """A number then "." at the start of a line is a list marker, not the end of a sentence."""
        line = self._visible[self._visible.rfind("\n", 0, period_end) + 1:period_end]
        return bool(re.fullmatch(r"\s*\d{1,2}[.)]", line))

    def _next_sentence(self) -> str | None:
        for m in _BOUNDARY.finditer(self._visible):
            if m.group(0) == "\n":
                end = m.end()
            else:
                if m.end() == len(self._visible) and not m.group(2):
                    continue  # "5." at the very end could still become "5.30"
                if self._is_abbreviation(m.start(1) + 1) or self._is_list_number(m.start(1) + 1):
                    continue
                end = m.end(1)
            sentence, self._visible = self._visible[:end], self._visible[end:]
            return sentence
        if len(self._visible) > self.max_chars:
            window = self._visible[: self.max_chars]
            cut = max(window.rfind(", "), window.rfind(" "))
            cut = cut if cut >= 40 else self.max_chars
            sentence, self._visible = self._visible[:cut + 1], self._visible[cut + 1:]
            return sentence
        return None

    def _release(self, sentence: str) -> str | None:
        text = _clean(self._carry + " " + sentence if self._carry else sentence)
        if not text or not re.search(r"\w", text):
            return None
        if self._emitted_any and len(text) < self.min_chars:
            self._carry = text
            return None
        self._carry = ""
        self._emitted_any = True
        return text

    def feed(self, fragment: str) -> list[str]:
        self._raw += fragment or ""
        self._filter()
        out: list[str] = []
        while (sentence := self._next_sentence()) is not None:
            if (text := self._release(sentence)) is not None:
                out.append(text)
        return out

    def flush(self) -> list[str]:
        """The end of the reply: whatever is left, including a carried fragment. A skipped block that never closed is dropped."""
        if self._skip is None:
            self._visible += _OPEN_TAG_TAIL.sub("", _ANY_TAG.sub("", self._raw))
        self._raw = ""
        self._skip = None
        out: list[str] = []
        tail = _clean((self._carry + " " + self._visible).strip())
        self._carry, self._visible = "", ""
        if tail and re.search(r"\w", tail):
            out.append(tail)
            self._emitted_any = True
        return out

"""
sdk.agent.memory_tools — saving a fact as an HTML-NN node

Extracted from tools.py to meet size ratchet.

A fact the user tells Tala becomes a real node in the live nodes folder, where retrieval can find it on the next
turn. Two doors lead here:

* ``write_memory`` -- the tool the model may call. It is registered in tools.py and gated by the lease policy like every
  other tool that writes a file.
* ``_remember_from_message`` -- the harness itself, for a message that *starts with* an explicit "remember ...". The user
  gave the instruction, so nothing waits on a small model choosing to call a tool; the same checks apply. The same door takes
  corrections ("actually my cat is named Pixel") and "forget that": a new fact about the same subject as a saved one retires the
  old one, and a forget command retires the fact it names. Retiring floors the node's weights so retrieval stops surfacing it; the
  file is kept. Every outcome comes back with a ``note`` for the model, so what she says about it is what happened.

Neither stores anything that looks like a credential, a wallet key or a seed phrase: a node is retrieval context and
is anchored to a ledger, so a secret saved here would be recalled into prompts and written down for good.

(This module used to INSERT into ``memory_index (node_id, content, topic_tags)`` -- columns that table has never had --
so the tool failed on every call and the HTML-NN store never saw a fact.)
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_FACT_CHARS = 2000
MIN_FACT_CHARS = 8

#: "remember that ...", "please remember: ...", "Tala, remember my ...". Anchored at the start: a sentence that merely
#: contains the word ("I remember that day", "what do you remember about ...") is not an instruction.
#: "remember to ..." is a reminder or an instruction, not a fact, and a system prompt can well begin "Remember to
#: ...", so it needs "that", a colon, or a possessive or article to count.
_EXPLICIT_REMEMBER = re.compile(
    r"^\s*(?:please\s+)?(?:tala[,:]?\s+)?remember(?:\s+that\b|\s*:|(?=\s+(?:my|our|i|we|the|this|these)\b))\s+(?P<fact>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
#: A fact is a sentence; anything much longer is a prompt or a document, never an instruction to remember one thing.
MAX_MESSAGE_CHARS = 600

#: "remember this: X", "remember the following - X": the lead-in is not part of the fact.
_LEAD_IN = re.compile(r"^(?:this|these|the\s+following)\s*[:\-\u2013\u2014]+\s*(?=\S)", re.IGNORECASE)
#: "..., you can drop it later", "(feel free to forget it afterwards)": an instruction about the fact, not the fact.
_DROP_TAIL = re.compile(r"[\s,;:.\-\u2013\u2014]*[(\[]?\s*(?:you\s+can|you\s+may|feel\s+free\s+to|please|go\s+ahead\s+and)\s+"
                        r"(?:drop|forget|delete|discard|ignore)\s+(?:it|that|this)\b[^)\]\n]*[)\]]?[.!\s]*$", re.IGNORECASE)
#: "actually my cat is ...", "no, it's ...", "that's wrong, ...", "I meant ...": a correction cue before the corrected statement.
_CORRECTION = re.compile(
    r"^\s*(?:(?:no|nope|actually|wait|sorry|correction|i\s+meant|i\s+mean|that'?s\s+(?:wrong|not\s+right|incorrect|not\s+correct)|"
    r"that\s+is\s+(?:wrong|not\s+right|incorrect))\s*[,:.!\-\u2013\u2014]*\s*)+(?P<fact>.+?)\s*$", re.IGNORECASE | re.DOTALL)
#: "forget that", "you can drop it", "forget what I just told you" -- the whole message, and short: "forget it, it's fine", "drop it like it's
#: hot" and "I'll forget that I asked" are not commands.
_FORGET_THAT = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright|thanks)[,.!]?\s*)?(?:please\s+)?(?:tala[,:]?\s+)?"
    r"(?:(?:(?:you\s+can|go\s+ahead\s+and)\s+)?(?:forget|drop|delete|discard)\s+(?:that|this|what\s+i\s+(?:just\s+)?(?:said|told\s+you))"
    r"|(?:you\s+can\s+|please\s+|go\s+ahead\s+and\s+)drop\s+it|drop\s+it)\s*[.!]*\s*$", re.IGNORECASE)
#: "forget my cat's name": a command naming what to forget.
_FORGET_SUBJECT = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright)[,.!]?\s*)?(?:please\s+)?(?:tala[,:]?\s+)?(?:you\s+can\s+)?(?:forget|drop|delete|discard)\s+(?:about\s+)?"
    r"(?P<subject>(?:my|our)\s+.+?)\s*[.!]*\s*$", re.IGNORECASE)
MAX_FORGET_CHARS = 80
#: "actually it's Mochi", "no, its called Mochi": a correction with no subject, so it means the fact last saved or recalled.
_ELLIPTICAL = re.compile(r"^(?:it|that|this)(?:\s*['\u2019]?s|\s+is)\s+(?:actually\s+|really\s+)?(?:(?:named|called)\s+)?(?P<value>[^,.;!?]{1,40}?)\s*[.!]*$",
                         re.IGNORECASE)
#: Not a replacement value: "no, it's fine" is not a new name.
_NOT_A_VALUE = frozenset("fine ok okay good great alright true right correct wrong false nothing nevermind whatever so not still the same "
                         "different better worse cool".split())
MAX_VALUE_WORDS = 3
#: A saved fact's value is what follows its last "is/are/named/called".
_VALUE_AT_END = re.compile(r"^(?P<head>.*?\b(?:is|are|named|called)\s+)(?P<old>[^,.;]+?)\s*$", re.IGNORECASE)
#: "forget that" means the fact last saved or recalled (the file changes on both) within the hour; found live 2026-10-30, when ten
#: minutes was too short: the cat had been asked about 17 minutes earlier, nothing was forgotten, and she said it was.
RECENT_SECONDS = 3600
#: Facts touched within this many seconds of each other were recalled together, so "that" does not say which one.
TOGETHER_SECONDS = 30
#: How long after a fact is retired the model is still told not to repeat it (the chat already on screen still holds the words).
RETIRED_NOTE_HOURS = 6
MAX_NOTED = 8
#: Two facts are about the same thing when their content words overlap this much (and share at least two).
SAME_SUBJECT = 0.5
SAME_SUBJECT_CUED = 0.34

_STOPWORDS = frozenset("a an the my our your his her their its is are was were be been am i me we you it of to and or in on at for with that this "
                       "so as by from has have had do does did not no".split())
_NAME_WORDS = frozenset({"named", "called", "name", "names"})

_CREDENTIAL_WORDS = re.compile(
    r"\b(password|passcode|passphrase|api[ _-]?key|secret[ _-]?key|private[ _-]?key|seed[ _-]?phrase|recovery[ _-]?phrase|"
    r"access[ _-]?token|auth[ _-]?token|bearer|ssn|social security|credit[ _-]?card|card number|cvv|wallet key)\b"
    r"|\btoken\s*[:=]|\bsecret\s*[:=]",
    re.IGNORECASE,
)


def _problem(text: str) -> str | None:
    """Why this text must not be saved as a fact, or None."""
    if not text:
        return "content cannot be empty"
    if len(text) > MAX_FACT_CHARS:
        return f"a fact can be at most {MAX_FACT_CHARS} characters"
    from sdk.gateway.redaction import redact_string

    if redact_string(text) != text or _CREDENTIAL_WORDS.search(text):
        return ("that looks like a credential, a key or other secret, so I won't store it in a node "
                "(nodes are recalled into prompts and written to the ledger)")
    return None


#: What write_memory puts before the user's words in the node (the parser folds the blank line into spaces).
_PREFIX = re.compile(r"^A fact the user told Tala \(topic:[^)]*\)\.\s*")


def _clean_fact(fact: str) -> str:
    """The fact as the user stated it: without a "this:" lead-in or a "you can drop it" tail."""
    fact = _DROP_TAIL.sub("", _LEAD_IN.sub("", fact.strip())).strip()
    return fact.rstrip(" ,;:-")


def _keys(text: str) -> set[str]:
    """The content words of a fact, reduced so "cat's name", "named" and "called" compare equal and "colour"/"colours" do."""
    words = re.sub(r"['\u2019]s\b", "", text.lower())
    out = set()
    for word in re.findall(r"[a-z0-9]+", words):
        if word in _NAME_WORDS:
            out.add("name")
        elif word not in _STOPWORDS:
            out.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return out


def _saved_facts() -> list[dict[str, Any]]:
    """The live fact nodes (newest first): id, path, text as the user said it, and when the file last changed. Retired ones are skipped."""
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    from sdk.agent.parser import parse_file

    facts = []
    for path in resolve_active_nodes_dir().glob("fact-*.html"):
        try:
            if 'data-retired="' in path.read_text(encoding="utf-8"):
                continue
            body = parse_file(str(path)).semantic_memory or ""
            facts.append({"id": path.stem, "path": path, "text": _PREFIX.sub("", body).strip(), "mtime": path.stat().st_mtime})
        except Exception as exc:
            log.debug("memory_tools: skipped unreadable fact %s: %s", path, exc)
    return sorted(facts, key=lambda fact: fact["mtime"], reverse=True)


#: A saved turn is about a retired fact when it carries this share of that fact's content words (and one that tells it apart from live facts).
RETIRED_RECALL_SHARE = 0.66


def without_retired(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``results`` minus what the user took back. A retired fact node is no longer recalled, but the memory index still holds the turn where it
    was said and the learning node made from her acknowledgement, and ``search_memory`` handed both to the model in a brand-new chat (found live
    2026-10-30: "what's my cat's name?" answered "Quillon" after the fact was superseded and then forgotten, first from the transcript turn,
    then from the retired learning node). Dropped: any result that points at a retired node, and any saved turn that restates a retired fact.
    Read fresh from the node files, so restoring or deleting a node by hand changes what is recalled. Never raises."""
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file

        directory = resolve_active_nodes_dir()
        retired, live = [], set()
        for path in directory.glob("fact-*.html"):
            keys = _keys(_PREFIX.sub("", parse_file(str(path)).semantic_memory or ""))
            if 'data-retired="' in path.read_text(encoding="utf-8"):
                retired.append(keys)
            else:
                live |= keys
        retired = [(keys, keys - live) for keys in retired if len(keys) >= 2]

        def retired_node(row: dict[str, Any]) -> bool:
            ref = str((row.get("ref") or {}).get("id") or "")
            path = directory / f"{ref}.html"
            return bool(re.fullmatch(r"[\w.-]+", ref)) and path.is_file() and 'data-retired="' in path.read_text(encoding="utf-8")

        def about_retired(row: dict[str, Any]) -> bool:
            if retired_node(row):
                return True
            if not retired or not str(row.get("source_type", "")).startswith("session"):
                return False
            said = _keys(str(row.get("snippet", "")))
            return any(said & told_apart and len(said & keys) / len(keys) >= RETIRED_RECALL_SHARE for keys, told_apart in retired)

        return [row for row in results if not about_retired(row)]
    except Exception as exc:
        log.debug("memory_tools: could not filter retired facts from recall: %s", exc)
        return results


def forgotten_note() -> str | None:
    """A system-context note naming the facts the user took back (forgot or replaced) in the last few hours, with the instruction not to use or
    repeat them. A retired node is no longer recalled, but the conversation already on screen still contains the words and the model repeats
    them (found live 2026-10-30: she answered "Quillon" after both cat facts were retired). Read fresh from the node files, so a fact the user
    restored or deleted by hand drops out. None when there is nothing to say; never raises."""
    try:
        from sdk.agent.documentation_nodes import resolve_active_nodes_dir
        from sdk.agent.parser import parse_file

        cutoff = datetime.now(timezone.utc) - timedelta(hours=RETIRED_NOTE_HOURS)
        directory = resolve_active_nodes_dir()
        lines = []
        for path in sorted(directory.glob("fact-*.html")):
            text = path.read_text(encoding="utf-8")
            kind, at = re.search(r'data-retired="(forgotten|superseded)"', text), re.search(r'data-retired-at="([^"]+)"', text)
            if not kind or not at or datetime.fromisoformat(at.group(1)) < cutoff:
                continue
            said = _shorten(_PREFIX.sub("", parse_file(str(path)).semantic_memory or "").strip(), 120)
            if kind.group(1) == "forgotten":
                lines.append(f'- forgotten: "{said}"')
                continue
            successor = re.search(r'data-superseded-by="([^"]+)"', text)
            now_says = ""
            if successor and (directory / f"{successor.group(1)}.html").is_file():
                now_says = f' (now: "{_shorten(_PREFIX.sub("", parse_file(str(directory / (successor.group(1) + ".html"))).semantic_memory or "").strip(), 120)}")'
            lines.append(f'- replaced: "{said}"{now_says}')
        if not lines:
            return None
        return ("=== FACTS THE USER TOOK BACK ===\nThe user recently told you to forget, or corrected, the facts below. Do not use or repeat them, "
                "even if they appear earlier in this conversation; if asked, say you no longer have them (or give the newer fact).\n"
                + "\n".join(lines[:MAX_NOTED]))
    except Exception as exc:
        log.debug("memory_tools: could not build the retired-facts note: %s", exc)
        return None


def _shorten(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _same_subject(new_text: str, cued: bool) -> list[dict[str, Any]]:
    """Saved facts about the same thing as ``new_text`` that say something different: they share two or more content words and enough of
    them (a correction cue lowers the bar), and are not the same statement."""
    new_keys = _keys(new_text)
    threshold = SAME_SUBJECT_CUED if cued else SAME_SUBJECT
    found = []
    for fact in _saved_facts():
        keys = _keys(fact["text"])
        shared = new_keys & keys
        if fact["text"].strip().lower() != new_text.strip().lower() and len(shared) >= 2 and len(shared) / len(new_keys | keys) >= threshold:
            found.append(fact)
    return found


def _retire(fact: dict[str, Any], kind: str, superseded_by: str | None = None, reason: str = "") -> bool:
    from sdk.agent import node_learning

    return bool(node_learning.retire(fact["id"], fact["path"].parent, kind=kind, superseded_by=superseded_by, reason=reason))


def _fact_node_id(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()[:6]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return "fact-" + "-".join(words or ["note"]) + f"-{digest}"


async def write_memory(content: str, topic: str = "user-preference") -> dict[str, Any]:
    """
    NAME
           write_memory - save a fact as an HTML-NN node

    SYNOPSIS
           write_memory(content: str, topic: str = "user-preference") -> dict[str, Any]

    DESCRIPTION
           Record a user preference, learned fact, or important insight as a node in the live nodes folder, so it can
           be recalled on the next turn. It starts at weight 0.80 (below the 1.00 ceiling) so that use and feedback
           can move it. The same fact twice is the same node. Credentials and secrets are refused.

           - content: The information to remember (required, at most 2000 characters)
           - topic: Optional category/topic tag (default: "user-preference")
    """
    text = str(content or "").strip()
    problem = _problem(text)
    if problem:
        return {"success": False, "error": problem}

    topic_tag = re.sub(r"[^a-z0-9-]+", "-", str(topic or "").strip().lower()).strip("-") or "user-preference"
    try:
        from sdk.agent import node_learning
        from sdk.agent.documentation_nodes import create_documentation_node, resolve_active_nodes_dir

        node_id = _fact_node_id(text)
        existing = resolve_active_nodes_dir() / f"{node_id}.html"
        if existing.is_file():
            # Already remembered. Regenerating it from the template would throw away what learning has done to it
            # (its weights, its usage count), so a fact told twice is left exactly as it is.
            return {"success": True, "node_id": node_id, "path": str(existing), "created": False,
                    "message": f"Already remembered as node '{node_id}'"}

        result = create_documentation_node(
            node_id=node_id,
            title=f"Fact: {text[:60]}",
            doc_target="memory.user-fact",
            semantic_memory=f"A fact the user told Tala (topic: {topic_tag}).\n\n{text}",
            node_type="documentation",
            tags=["html-nn", "memory", "user-fact", topic_tag],
            stage=False,
        )
        if result.get("errors") or not result.get("validation_passed"):
            return {"success": False, "error": f"the node did not validate: {result.get('errors')}"}
        if result.get("created"):
            node_learning.seed_new_node(Path(result["path"]), relevance=node_learning.NEW_FACT_WEIGHT,
                                        confidence=node_learning.NEW_FACT_WEIGHT)
        return {
            "success": True,
            "node_id": result["node_id"],
            "path": result["path"],
            "created": bool(result.get("created")),
            "message": f"Saved as node '{result['node_id']}' under topic '{topic_tag}'",
        }
    except Exception as exc:
        return {"success": False, "error": f"Could not write the node: {exc}"}


async def _save_fact(fact: str, *, cued: bool, require_conflict: bool) -> dict[str, Any] | None:
    """Save ``fact``; if it restates something already saved, the older statement is retired. With ``require_conflict`` (a bare correction
    cue) nothing is saved unless there is a fact to correct: "no, I don't want that" is not a memory."""
    fact = _clean_fact(fact)
    if len(fact) < MIN_FACT_CHARS or fact.endswith("?"):
        return None
    older = _same_subject(fact, cued) if not _problem(fact) else []
    if require_conflict and not older:
        return None
    # The nodes are files the user can edit: one they changed to say exactly this is already the memory, whatever its id says.
    same = next((saved for saved in _saved_facts() if saved["text"].strip().lower() == fact.lower()), None) if not _problem(fact) else None
    if same:
        return {"success": True, "node_id": same["id"], "path": str(same["path"]), "created": False,
                "note": f'[Memory] The user told you "{_shorten(fact)}", which is already saved; nothing changed.'}
    result = await write_memory(fact, "user-fact")
    if not result.get("success"):
        return result
    if not result.get("created") or not older:
        result["note"] = f'[Memory] Saved a fact the user just told you: "{_shorten(fact)}". You may say you will remember it.'
        return result
    retired = [old for old in older if _retire(old, "superseded", result["node_id"], f"replaced by: {fact[:120]}")]
    old_text = "; ".join(f'"{_shorten(old["text"], 100)}"' for old in older)
    result["retired"] = [old["id"] for old in retired]
    result["note"] = (f'[Memory] Saved: "{_shorten(fact)}". It replaces an earlier fact, {old_text}, which is retired and will no longer be '
                      f"recalled." if retired else
                      f'[Memory] Saved: "{_shorten(fact)}". It may contradict an earlier fact, {old_text}, but learning is off or halted so '
                      "the old one could not be retired; say that.")
    return result


def _forget_note(text: str) -> dict[str, Any]:
    return {"success": False, "note": text}


def _forget(facts: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    fact = facts[0]
    if not _retire(fact, "forgotten", None, reason):
        return _forget_note('[Memory] The user asked you to forget a fact, but it could not be retired because learning is off or halted; '
                            "say so and change nothing. Do NOT say you forgot anything.")
    return {"success": True, "node_id": fact["id"], "retired": [fact["id"]],
            "note": f'[Memory] Forgotten at the user\'s request: "{_shorten(fact["text"])}". It is retired and will no longer be recalled '
                    "(the file is kept)."}


async def _forget_from_message(message: str) -> dict[str, Any] | None:
    """"forget that" / "you can drop it" (the fact saved in the last few minutes) or "forget my cat's name" (the one fact that matches it)."""
    pronoun = _FORGET_THAT.match(message) if len(message) <= MAX_FORGET_CHARS else None
    subject = None if pronoun or len(message) > MAX_FORGET_CHARS else _FORGET_SUBJECT.match(message)
    if not pronoun and not subject:
        return None
    facts = _saved_facts()
    if pronoun:
        recent = [fact for fact in facts if time.time() - fact["mtime"] <= RECENT_SECONDS]
        if not recent:
            return _forget_note("[Memory] The user asked you to forget something, but no fact was saved or recalled in the last hour to forget; "
                                "change nothing and say so (ask what they mean). Do NOT say you forgot anything.")
        together = [fact for fact in recent if recent[0]["mtime"] - fact["mtime"] <= TOGETHER_SECONDS]
        if len(together) > 1:
            names = "; ".join(f'"{_shorten(fact["text"], 80)}"' for fact in together)
            return _forget_note(f"[Memory] The user asked you to forget \"that\", but several facts were just recalled together ({names}); nothing was "
                                "changed. Ask which one. Do NOT say you forgot anything.")
        return _forget(recent[:1], "user said to forget it")
    wanted = _keys(subject.group("subject"))
    scored = sorted(((len(wanted & _keys(fact["text"])) / len(wanted), fact) for fact in facts if wanted), key=lambda pair: pair[0], reverse=True)
    scored = [(score, fact) for score, fact in scored if score >= 0.5]
    if not scored:
        return None  # nothing saved is about that: an ordinary sentence, not a memory command
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        names = "; ".join(f'"{_shorten(fact["text"], 80)}"' for score, fact in scored if score == scored[0][0])
        return _forget_note(f"[Memory] The user asked you to forget something that matches more than one saved fact ({names}); nothing was "
                            "changed. Ask which one. Do NOT say you forgot anything.")
    return _forget([scored[0][1]], f"user said to forget: {subject.group('subject')[:100]}")


async def _correct_last_fact(value: str) -> dict[str, Any] | None:
    """"actually it's Mochi": the fact last saved or recalled, with its value replaced. None when there is no such fact or the reply is not a value
    (a bare "no, it's fine" must not rewrite a fact); a note, changing nothing, when it cannot be told which fact or which part to change."""
    words = re.findall(r"[A-Za-z0-9']+", value)
    if not words or len(words) > MAX_VALUE_WORDS or any(word.lower() in _NOT_A_VALUE for word in words):
        return None
    recent = [fact for fact in _saved_facts() if time.time() - fact["mtime"] <= RECENT_SECONDS]
    if not recent:
        return None
    if len([fact for fact in recent if recent[0]["mtime"] - fact["mtime"] <= TOGETHER_SECONDS]) > 1:
        return _forget_note("[Memory] The user corrected something with \"it's " + value + "\", but several facts were just recalled together, so "
                            "nothing was changed. Ask which one. Do NOT say you updated or remembered anything.")
    old = recent[0]
    parts = _VALUE_AT_END.match(old["text"])
    if not parts:
        return _forget_note(f'[Memory] The user corrected "{_shorten(old["text"])}" with "it\'s {value}", but I could not tell which part to change, so '
                            "nothing was changed. Ask them to say the whole fact. Do NOT say you updated or remembered anything.")
    return await _save_fact(parts.group("head") + value.strip(), cued=True, require_conflict=True)


async def _remember_from_message(message: Any) -> dict[str, Any] | None:
    """If the user's message is an explicit "remember ...", a correction of a saved fact, or "forget ...", act on it now. None when it is none of these."""
    if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
        return None
    forgotten = await _forget_from_message(message)
    if forgotten is not None:
        return forgotten
    found = _EXPLICIT_REMEMBER.match(message)
    if found:
        return await _save_fact(found.group("fact"), cued=False, require_conflict=False)
    cue = _CORRECTION.match(message)
    if cue:
        fact = cue.group("fact")
        restated = _EXPLICIT_REMEMBER.match(fact)  # "no, remember that my cat is Pixel"
        bare = None if restated else _ELLIPTICAL.match(fact)
        if bare:
            return await _correct_last_fact(bare.group("value"))
        return await _save_fact(restated.group("fact") if restated else fact, cued=True, require_conflict=True)
    return None


async def remember_safely(message: Any) -> dict[str, Any] | None:
    """``_remember_from_message`` for the turn pipeline: a failure to save a note costs the note, never the reply."""
    try:
        return await _remember_from_message(message)
    except Exception as exc:
        log.warning("_remember_from_message failed: %s", exc)
        return None

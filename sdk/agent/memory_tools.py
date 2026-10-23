"""
sdk.agent.memory_tools — saving a fact as an HTML-NN node

Extracted from tools.py to meet size ratchet.

A fact the user tells Tala becomes a real node in the live nodes folder, where retrieval can find it on the next
turn. Two doors lead here:

* ``write_memory`` -- the tool the model may call. It is registered in tools.py and gated by the lease policy like every
  other tool that writes a file.
* ``remember_from_message`` -- the harness itself, for a message that *starts with* an explicit "remember ...". The user
  gave the instruction, so nothing waits on a small model choosing to call a tool; the same checks apply.

Neither stores anything that looks like a credential, a wallet key or a seed phrase: a node is retrieval context and
is anchored to a ledger, so a secret saved here would be recalled into prompts and written down for good.

(This module used to INSERT into ``memory_index (node_id, content, topic_tags)`` -- columns that table has never had --
so the tool failed on every call and the HTML-NN store never saw a fact.)
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

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


async def remember_from_message(message: Any) -> dict[str, Any] | None:
    """If the user's message is an explicit "remember ...", save that fact now. None when it is not one."""
    found = (_EXPLICIT_REMEMBER.match(message)
             if isinstance(message, str) and len(message) <= MAX_MESSAGE_CHARS else None)
    if not found:
        return None
    fact = found.group("fact").strip()
    if len(fact) < MIN_FACT_CHARS or fact.endswith("?"):
        return None
    return await write_memory(fact, "user-fact")

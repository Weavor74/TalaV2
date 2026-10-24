"""
python/harness/peer_billing.py -- who, if anyone, a request is billed to.

``inference.generate`` used the first ``@word`` anywhere in the prompt as the resource owner. In ``vps`` mode a peer other than
yourself is paid in real tokens, so a decorator, an email address or a handle in a file the model had just read turned a call to
the user's own model into a payment to a stranger, and an empty wallet refused it. Only a registered contact is a peer.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_HANDLE = re.compile(r"@([a-zA-Z0-9_-]+)")
#: A code file can hold hundreds of ``@decorators``; look up the first few distinct ones, not each of them.
MAX_LOOKUPS = 8


async def known_peer(text: str, get_contact: Callable[..., Awaitable[Any]]) -> str | None:
    """``"@name"`` for the first handle in ``text`` that is a registered contact, else None (nobody is paid)."""
    tried: set[str] = set()
    for match in _HANDLE.finditer(text or ""):
        name = match.group(1)
        if name in tried:
            continue
        if len(tried) >= MAX_LOOKUPS:
            break
        tried.add(name)
        try:
            contact = await get_contact(name)
        except Exception as exc:
            log.debug("peer lookup for @%s failed: %s", name, exc)
            continue
        if contact:
            return f"@{name}"
    return None

"""
vault_tools.py — the local model can reference vault documents.

Steven: *"Things the local LLM could reference, but nothing gets shared
outside."* The first half had no mechanism (UI-038): no `vault_read`, no
`vault_list`, no `vault_search`, and no generic RPC-invoke tool, so Tala had no
way to know a vault document existed, let alone read one.

The second half is enforced here rather than assumed, to the rules Steven set:
a body may not reach a **remote** model unless that document carries an
explicit grant, checked per document by :func:`assert_may_read`. Listing is not
gated -- a vault you cannot see the titles of is one you cannot ask about --
and local models read freely, which is the whole point of "things the local LLM
could reference".

Read-only, deliberately, matching the same split used for logs and PII: Tala
can reference what is in the vault; putting things in and taking them out stays
with the user. A private document store an agent can silently rewrite is not a
private document store.
"""

from __future__ import annotations

import logging

from sdk.agent.tools import register_tool

log = logging.getLogger(__name__)


@register_tool(
    name="vault_list",
    description=(
        "List the user's private vault documents: id, title and tags. Never "
        "returns contents -- use vault_read to open one."
    ),
)
async def vault_list() -> dict:
    """
    NAME
           vault_list - what private documents exist

    SYNOPSIS
           vault_list() -> dict

    DESCRIPTION
           Titles and tags only. Enough to know a document exists and decide
           whether to open it, without opening everything to find out.

           Not gated on the active model: a vault whose titles cannot be seen
           is a vault nobody can ask about. The bodies are what the grant
           governs.
    """
    from python.harness.vault_nodes import list_documents

    # Listing is not gated: titles are what makes the vault usable at all, and
    # a list you cannot see is a vault you cannot ask about. The bodies are
    # what the grant governs.
    documents = list_documents()
    return {
        "success": True,
        "count": len(documents),
        "documents": documents,
        "note": (
            "The vault is empty."
            if not documents else
            "Contents are sealed; use vault_read to open one."
        ),
    }


@register_tool(
    name="vault_read",
    description=(
        "Read one private vault document by id. Refused when the active model "
        "is remote unless that document carries an explicit grant."
    ),
)
async def vault_read(doc_id: str) -> dict:
    """
    NAME
           vault_read - open one private document

    SYNOPSIS
           vault_read(doc_id: str) -> dict

    DESCRIPTION
           Unseals and returns the document body. A local model reads freely.
           A remote model is refused unless the user has granted that specific
           document remote access, and the refusal says so rather than
           returning empty -- a refusal that looks like an empty document
           teaches the caller the wrong thing.

    PARAMETERS
           doc_id - the document id from vault_list, with or without the
                    "vault-" prefix
    """
    from python.harness.vault_nodes import (
        VaultEgressError, assert_may_read, read_document,
    )

    try:
        await assert_may_read(doc_id)
    except VaultEgressError as exc:
        # Said as a refusal, not as an empty document: a refusal that looks
        # like "there is nothing here" teaches the caller the wrong thing.
        return {"success": False, "error": str(exc), "refused": True}
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    try:
        return read_document(doc_id)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="vault_search",
    description=(
        "Search inside the user's private vault documents for a phrase. "
        "Documents without a grant are withheld when the active model is remote."
    ),
)
async def vault_search(query: str) -> dict:
    """
    NAME
           vault_search - find a phrase across private documents

    SYNOPSIS
           vault_search(query: str) -> dict

    DESCRIPTION
           Vault bodies ARE indexed locally into ChromaDB, by Steven's
           decision: the index holds plaintext and never leaves the machine,
           which is the trade that makes the vault findable rather than merely
           storable. This tool opens the documents directly rather than going
           through that index, so a result is always the current file.

           Each hit is checked individually, so one withheld document does not
           hide the rest.
    """
    from python.harness.vault_nodes import (
        VaultEgressError, assert_may_read, search_documents,
    )

    # Searching reads bodies, so each hit is checked individually -- a document
    # granted remote access is searchable while one that is not stays closed,
    # rather than one refusal hiding the whole vault.
    hits = []
    refused = 0
    for hit in search_documents(query):
        try:
            await assert_may_read(hit["id"])
        except (VaultEgressError, FileNotFoundError):
            refused += 1
            continue
        hits.append(hit)
    return {
        "success": True,
        "count": len(hits),
        "hits": hits,
        "withheld": refused,
        "note": (
            f"{refused} matching document(s) were withheld: the active model is "
            f"remote and they carry no grant."
            if refused else
            "No vault document contains that phrase."
            if not hits else ""
        ),
    }

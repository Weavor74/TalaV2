"""
vault_nodes.py — vault documents are HTML-NN labelled vault.

Steven, on the Vault view:

    "Vault section does not work at all. It is meant for storage of editable
    private documents. Things the local LLM could reference, but nothing gets
    shared outside."

and, on the framework to build it on:

    "We started that framework with the html-nn being labeld as vault"

So a vault document is not a second kind of storage. It is an HTML-NN with
``data-node-type="vault"``, sealed at rest by :mod:`python.harness.node_vault`,
living under the portable data root like every other node. That keeps the
HTML-NN mandate intact and means the vault inherits everything nodes already
have: a parser, a schema validator, relationship anchors, and provenance.

The rules, as Steven decided them (2026-08-23). "Nothing gets shared outside"
has four different edges and they do not get the same answer:

* **Remote models: refused by default, allowed by explicit grant.** Putting a
  vault document into a prompt bound for a hosted model is sharing it outside,
  so it is refused -- but per-document, revocable grants exist, because a
  blanket refusal makes the vault unusable on the days you are working on a
  hosted model. Default deny, opt in per document.
* **Local search: yes.** Vault bodies are indexed into ChromaDB so retrieval
  can actually find them. This is a deliberate trade Steven made: the index
  holds plaintext, and the index never leaves the machine. It is the reason
  "things the local LLM could reference" is true rather than aspirational.
* **Backup and sync: never.** The vault does not leave the install, sealed or
  otherwise. Storage providers do not see it. The cost is real -- losing the
  drive loses the vault -- and it is the cost Steven chose.
* **Contacts: only by explicit per-document invite.** Consistent with the rest
  of the permission model: a contact grant does not reach the vault, and
  nothing is shared unless a named document is offered to a named contact.

The old design got the shape wrong before any of this: it gated *reads* on a
global network-isolation flag that was permanently false (UI-102), so the vault
was unreachable by construction -- and had the flag worked, the vault would
have been unusable whenever the app was online. Sharing is a property of the
**exit**, not of reading.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VAULT_NODE_TYPE = "vault"

#: Vault documents live beside the other nodes, under the portable data root.
#: A separate tree would be a second store, which is the thing this avoids.
VAULT_PREFIX = "vault-"


class VaultEgressError(PermissionError):
    """Raised when vault content would leave the machine."""


def _nodes_dir() -> Path:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    return resolve_active_nodes_dir()


def _safe_id(title: str) -> str:
    """
    A document id from a title, or an already-formed id passed straight back.

    The prefix was applied unconditionally, so re-saving a document by its own
    id produced vault-vault-<name> -- a second file. Grants written that way
    landed somewhere nothing ever read, and the original document silently kept
    its old permissions, which is the worst possible way for a permission edit
    to fail.
    """
    raw = str(title or "").strip()
    if raw.startswith(VAULT_PREFIX):
        raw = raw[len(VAULT_PREFIX):]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", raw.lower()).strip("-")
    if not slug:
        raise ValueError("a vault document needs a title with at least one letter or digit")
    return f"{VAULT_PREFIX}{slug[:60]}"


def _path_for(doc_id: str) -> Path:
    doc_id = str(doc_id or "").strip()
    if not doc_id.startswith(VAULT_PREFIX):
        doc_id = f"{VAULT_PREFIX}{doc_id}"
    # Containment: a document id must not be able to climb out of nodes/.
    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        raise ValueError(f"invalid vault document id: {doc_id!r}")
    return _nodes_dir() / f"{doc_id}.html"


# -- egress ------------------------------------------------------------------

def is_vault_node(path: Path | str) -> bool:
    """Whether a node file is a vault document, read from the file itself."""
    try:
        html = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return f'data-node-type="{VAULT_NODE_TYPE}"' in html


# -- write -------------------------------------------------------------------

def write_document(
    title: str,
    body: str,
    tags: list[str] | None = None,
    doc_id: str | None = None,
    grants: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create or replace a vault document.

    The body is sealed before it touches disk. The title and tags are not: a
    vault whose contents cannot be listed without the key is a vault nobody can
    use, and the title is what the user picked to recognise it by.
    """
    from python.harness.credential_crypto import encrypt_section, load_or_create_key
    import html as _html
    import json

    node_id = _safe_id(doc_id or title)
    path = _path_for(node_id)

    if load_or_create_key() is None:
        raise RuntimeError(
            "No encryption key is available, so this document cannot be sealed. "
            "Refusing to write it in clear."
        )

    sealed = encrypt_section({"body": str(body or "")})
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "title": str(title or "").strip(),
        "tags": [str(t) for t in (tags or [])],
        "vault_sealed": True,
        "updated_at": now,
        # Default deny. A document with no grants block is closed.
        "grants": grants or {"remote_inference": False, "contacts": []},
        "sealed": sealed,
    }

    safe_title = _html.escape(state["title"])
    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <title>Vault: {safe_title}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="{VAULT_NODE_TYPE}"
  data-execution-authority="0.0"
  data-memory-authority="0.0"
  data-weight-relevance="1.0"
  data-vault="true">
  <header id="node-identity"><h1 id="node-title">Vault: {safe_title}</h1></header>
  <section id="semantic-memory">
    <p>A private vault document titled "{safe_title}". Its contents are
    encrypted at rest and are deliberately not reproduced here: this file is
    readable by anyone holding it, and the semantic-memory section is what
    retrieval would index. Read it with the vault tools, which require a local
    model.</p>
  </section>
  <script type="application/json" id="node-state">{json.dumps(state, indent=2)}</script>
</article>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    log.info("vault: wrote %s (%d bytes sealed)", node_id, len(str(body or "")))
    return {"id": node_id, "title": state["title"], "path": str(path),
            "updated_at": now, "bytes": len(str(body or ""))}


# -- read --------------------------------------------------------------------

def list_documents() -> list[dict[str, Any]]:
    """
    Every vault document: id, title, tags, size. Never the contents.

    Listing does not require the key, and deliberately so -- ``vault.status``
    returned only a count, so even a finished UI could not have enumerated the
    vault (UI-037).
    """
    from sdk.agent.parser import parse_file

    out: list[dict[str, Any]] = []
    for path in sorted(_nodes_dir().glob(f"{VAULT_PREFIX}*.html")):
        try:
            state = parse_file(str(path)).state_data or {}
        except Exception as exc:
            log.warning("vault: could not parse %s: %s", path.name, exc)
            out.append({"id": path.stem, "title": path.stem, "unreadable": True,
                        "error": f"{type(exc).__name__}"})
            continue
        grants = state.get("grants")
        out.append({
            "id": path.stem,
            "title": state.get("title") or path.stem,
            "tags": state.get("tags") or [],
            "updated_at": state.get("updated_at", ""),
            "sealed": bool(state.get("vault_sealed")),
            "bytes": path.stat().st_size,
            # Carried on the listing so a panel can show what is actually
            # stored rather than what the user last typed. Without it the
            # access controls render as "off" whatever they are set to, which
            # is the most dangerous direction for that particular mistake.
            "grants": {
                "remote_inference": bool((grants or {}).get("remote_inference")),
                "contacts": list((grants or {}).get("contacts") or []),
            },
        })
    return out


def read_document(doc_id: str) -> dict[str, Any]:
    """Unseal and return one vault document's body."""
    from python.harness.credential_crypto import decrypt_section
    from sdk.agent.parser import parse_file

    path = _path_for(doc_id)
    if not path.exists():
        return {"success": False, "error": f"No vault document named {doc_id!r}."}

    try:
        state = parse_file(str(path)).state_data or {}
    except Exception as exc:
        return {"success": False, "error": f"Could not parse {path.name}: {exc}"}

    sealed = state.get("sealed") or {}
    opened = decrypt_section(sealed) if isinstance(sealed, dict) else {}
    body = opened.get("body")
    if body is None:
        return {
            "success": False,
            "error": (
                "The document is sealed and could not be opened. The encryption "
                "key is missing or does not match."
            ),
        }
    return {
        "success": True,
        "id": path.stem,
        "title": state.get("title") or path.stem,
        "tags": state.get("tags") or [],
        "updated_at": state.get("updated_at", ""),
        "body": body,
    }


def search_documents(query: str) -> list[dict[str, Any]]:
    """
    Search vault contents, which means opening each one.

    Unavoidable: the whole point is that the bodies are not indexed anywhere a
    search engine could reach, so there is no index to consult. Vault size is
    measured in documents a person wrote, not millions of rows.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return []

    hits = []
    for entry in list_documents():
        if entry.get("unreadable"):
            continue
        doc = read_document(entry["id"])
        if not doc.get("success"):
            continue
        body = str(doc.get("body") or "")
        if needle in body.lower() or needle in str(doc.get("title", "")).lower():
            idx = body.lower().find(needle)
            start = max(0, idx - 60)
            hits.append({
                "id": doc["id"],
                "title": doc["title"],
                "snippet": (body[start:start + 200] if idx >= 0 else body[:200]),
            })
    return hits


def delete_document(doc_id: str) -> dict[str, Any]:
    """Remove a vault document. The user's call, never the agent's."""
    path = _path_for(doc_id)
    if not path.exists():
        return {"success": False, "error": f"No vault document named {doc_id!r}."}
    path.unlink()
    log.info("vault: deleted %s", path.stem)
    return {"success": True, "id": path.stem}


# -- grants ------------------------------------------------------------------
#
# A grant is recorded on the document itself, so it travels with the thing it
# governs and there is no second store to fall out of sync. Default is always
# deny: a document with no grants block is closed, and a grant that cannot be
# parsed is treated as absent rather than as permission.

def _load_state(doc_id: str) -> tuple[Path, dict[str, Any]]:
    from sdk.agent.parser import parse_file

    path = _path_for(doc_id)
    if not path.exists():
        raise FileNotFoundError(f"No vault document named {doc_id!r}.")
    return path, (parse_file(str(path)).state_data or {})


def get_grants(doc_id: str) -> dict[str, Any]:
    """The grants recorded on a document. Absent or malformed means none."""
    try:
        _, state = _load_state(doc_id)
    except (FileNotFoundError, Exception):
        return {"remote_inference": False, "contacts": []}
    grants = state.get("grants")
    if not isinstance(grants, dict):
        return {"remote_inference": False, "contacts": []}
    contacts = grants.get("contacts")
    return {
        "remote_inference": bool(grants.get("remote_inference")),
        "contacts": [str(c).strip().lstrip("@").lower()
                     for c in contacts if str(c).strip()] if isinstance(contacts, list) else [],
    }


def set_grant(doc_id: str, remote_inference: bool | None = None,
              contacts: list[str] | None = None) -> dict[str, Any]:
    """
    Record or revoke grants on a vault document.

    Rewrites the document rather than patching the file in place, so the body
    is re-sealed with the same code path that wrote it. A grant edit must not
    be a second, subtly different writer.
    """
    doc = read_document(doc_id)
    if not doc.get("success"):
        return doc

    grants = get_grants(doc_id)
    if remote_inference is not None:
        grants["remote_inference"] = bool(remote_inference)
    if contacts is not None:
        grants["contacts"] = [
            str(c).strip().lstrip("@").lower() for c in contacts if str(c).strip()
        ]

    result = write_document(
        title=doc["title"], body=doc["body"], tags=doc.get("tags") or [],
        doc_id=doc["id"], grants=grants,
    )
    log.info("vault: grants updated on %s -> %s", doc["id"], grants)
    return {"success": True, "id": doc["id"], "grants": grants, **result}


async def assert_may_read(doc_id: str, reason: str = "") -> None:
    """
    Refuse if reading this document here would share it outside.

    Only the remote-inference edge is checked: a local model reading a vault
    document is the intended use, and the other three edges are enforced where
    they happen -- sync excludes the vault outright, contact sharing needs a
    per-document invite.
    """
    from python.harness import db

    try:
        row = await db.fetchrow(
            "SELECT type, name FROM inference_providers WHERE status = 'online' "
            "ORDER BY priority ASC LIMIT 1"
        )
    except Exception as exc:
        # Fail closed. Not knowing where a prompt is going is not permission to
        # put a private document into it.
        raise VaultEgressError(
            f"Refusing to open {doc_id!r}: could not determine which inference "
            f"provider is active ({type(exc).__name__})."
        ) from exc

    if not row or str(row["type"]).lower() != "remote":
        return

    if get_grants(doc_id).get("remote_inference"):
        log.warning(
            "vault: %s read while the remote provider %r is active, under an "
            "explicit grant.", doc_id, row["name"],
        )
        return

    raise VaultEgressError(
        f"Refusing to open {doc_id!r}: the active provider {row['name']!r} is "
        f"remote, and vault content does not leave this machine without an "
        f"explicit grant. Either switch to a local model, or grant this "
        f"document remote access in the Vault panel."
    )


def may_share_with_contact(doc_id: str, contact: str) -> bool:
    """
    Whether one named contact may receive one named document.

    Deliberately not derived from the contact's general permissions: a contact
    grant does not reach the vault, so a misconfigured permission cannot
    disclose a private document.
    """
    who = str(contact or "").strip().lstrip("@").lower()
    return bool(who) and who in get_grants(doc_id)["contacts"]

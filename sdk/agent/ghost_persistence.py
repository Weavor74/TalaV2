"""
NAME
       ghost_persistence - Offline-first thought-state persistence for agent loops

SYNOPSIS
       from sdk.agent.ghost_persistence import commit_to_ghost, restore_from_ghost

DESCRIPTION
       ghost_persistence provides serialization and recovery of the agent's
       internal thought-state, process context, and variable scopes using the
       local Ghost Blockchain node.
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

from sdk.agent.anchor import _load_or_create_document_key

log = logging.getLogger(__name__)

def _decrypt_document(encrypted_document: dict) -> dict:
    """
    NAME
           _decrypt_document - Decrypt AES-256-GCM encrypted document
           
    SYNOPSIS
           plaintext = _decrypt_document(encrypted_document)
           
    DESCRIPTION
           Decrypts the ciphertext payload using the local vault document key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    
    key = _load_or_create_document_key()
    aesgcm = AESGCM(key)
    
    nonce = base64.b64decode(encrypted_document["nonce"])
    ciphertext = base64.b64decode(encrypted_document["ciphertext"])
    tag = base64.b64decode(encrypted_document["tag"])
    aad = encrypted_document["aad"].encode("utf-8")
    
    ciphertext_and_tag = ciphertext + tag
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, aad)
    return json.loads(plaintext_bytes.decode("utf-8"))

async def commit_to_ghost(
    task_id: str,
    process_context: dict[str, Any],
    variable_state: dict[str, Any],
    intended_next_step_hash: str,
) -> str:
    """
    NAME
           commit_to_ghost - Commit agent thought-state to the local Ghost Blockchain
           
    SYNOPSIS
           tx_id = await commit_to_ghost(task_id, process_context, variable_state, intended_next_step_hash)
           
    DESCRIPTION
           Saves thought-state metadata to the PoA blockchain node under a
           'thought-state' document type.
    """
    from sdk.agent.anchor import canonicalize_and_anchor

    node_id = f"ghost_commit_{task_id}"
    content_html = f"<div>Thought-state snapshot for task: {task_id}</div>"
    state_data = {
        "task_id": task_id,
        "process_context": process_context,
        "variable_state": variable_state,
        "intended_next_step_hash": intended_next_step_hash,
    }

    log.info("GhostPersistence: Committing thought-state to local blockchain for task %s", task_id)
    
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        node_id=node_id,
        content_html=content_html,
        state_data=state_data,
        document_type="thought-state",
        creator="tala-agent-loop",
    )
    return chaincode_id

async def restore_from_ghost(task_id: str) -> dict[str, Any] | None:
    """
    NAME
           restore_from_ghost - Restore agent thought-state from local Ghost Blockchain
           
    SYNOPSIS
           state = await restore_from_ghost(task_id)
           
    DESCRIPTION
           Reads the local chain.json directly, finds the latest transaction
           for this task of type 'thought-state', decrypts it, and returns the
           state. If the local file does not have it -- which happens whenever
           the running chain node's data directory is not the one TALA_USER_DATA
           points at (a sandboxed test, or a node started from a different root)
           -- it falls back to asking the node over HTTP, since commit_to_ghost
           anchors through that same node and it therefore always holds the tx.
    """
    log.info("GhostPersistence: Attempting to restore thought-state for task %s", task_id)

    state = _restore_from_local_file(task_id)
    if state is not None:
        return state
    return _restore_from_node_http(task_id)


def _restore_from_local_file(task_id: str) -> dict[str, Any] | None:
    """Read chain.json directly (fully local and fast). None if not present."""
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        data_dir = Path(user_data) / "blockchain" / "data"
    else:
        # Walk up from this file to find blockchain/data
        repo_root = Path(__file__).resolve().parent.parent.parent
        data_dir = repo_root / "blockchain" / "data"

    chain_file = data_dir / "chain.json"
    if not chain_file.exists():
        log.info("GhostPersistence: no local chain.json at %s; will try the node over HTTP", chain_file)
        return None

    try:
        with open(chain_file, "r", encoding="utf-8") as f:
            blocks = json.load(f)

        # Search backward from the latest blocks
        for block in reversed(blocks):
            for tx in block.get("transactions", []):
                if tx.get("id") == f"ghost_commit_{task_id}" and tx.get("metadata", {}).get("document_type") == "thought-state":
                    doc = tx.get("document")
                    if doc:
                        return _decrypt_document(doc).get("state_data")
    except Exception as exc:
        log.warning("GhostPersistence: Failed to read local chain.json: %s", exc)

    return None


def _restore_from_node_http(task_id: str) -> dict[str, Any] | None:
    """
    Ask the running chain node for the tx directly.

    commit_to_ghost anchors through the node, so it holds the transaction even
    when the local chain.json lives elsewhere. GET /chain/tx/:id returns
    ``{"transaction": {...}}`` including the (encrypted) document.
    """
    url = f"{local_chain_base_url()}/chain/tx/ghost_commit_{task_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log.warning("GhostPersistence: HTTP restore failed for task %s: %s", task_id, exc)
        return None

    tx = (payload or {}).get("transaction") or {}
    if tx.get("metadata", {}).get("document_type") != "thought-state":
        return None
    doc = tx.get("document")
    if not doc:
        return None
    try:
        return _decrypt_document(doc).get("state_data")
    except Exception as exc:
        log.warning("GhostPersistence: could not decrypt restored document: %s", exc)
        return None

def _get_local_status() -> dict[str, Any]:
    """
    NAME
           get_local_status - Query local blockchain status
           
    SYNOPSIS
           status = _get_local_status()
           
    DESCRIPTION
           Hits the local status endpoint.
    """
    try:
        url = f"{local_chain_base_url()}/chain/status"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}

def _get_json(url: str) -> Any:
    import json
    import ssl
    import urllib.request

    # Certificates are verified. This previously set check_hostname = False and
    # verify_mode = CERT_NONE, which means anything on the path could answer as
    # the remote chain -- and what comes back is chain data that was then fed
    # straight into the local node. An unverified transport carrying blocks is
    # not a transport problem, it is a chain-integrity problem.
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "TalaClient/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))

def _post_json(url: str, payload: dict, extra_headers: dict[str, str] | None = None) -> Any:
    import json
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "TalaClient/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def local_chain_host_port() -> tuple[str, int]:
    """Where the local chain node listens: the same variables blockchain/src/app.js reads.

    A wildcard bind address is a place to listen, not to connect to, so it
    maps to loopback. Every caller used to write 127.0.0.1:8332 into itself,
    so a portable install that moved the port silently lost its anchoring.
    """
    host = os.environ.get("TALA_CHAIN_BIND_HOST") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    raw = os.environ.get("TALA_CHAIN_BIND_PORT") or os.environ.get("TALA_CHAIN_PORT") or "8332"
    try:
        port = int(raw)
    except ValueError:
        port = 8332
    return host, port


def local_chain_base_url() -> str:
    host, port = local_chain_host_port()
    return f"http://{host}:{port}"


def _local_chain_admin_headers() -> dict[str, str]:
    """
    Admin secret for the LOCAL blockchain node only (blockchain/data/admin_secret.txt,
    auto-generated by blockchain/src/app.js on first run). Never send this to a
    remote host — it authenticates "this caller can read files on this machine",
    not a network identity.
    """
    secret = os.environ.get("TALA_CHAIN_ADMIN_SECRET", "")
    if not secret:
        # Read the secret from wherever the node actually keeps it. app.js
        # resolves its data dir as TALA_CHAIN_DATA_DIR, else relative to its
        # own location -- which on a portable install is the user-data root.
        # Reading only the repo path meant a node running against any other
        # directory minted one secret while this helper read another, and
        # every local submission 401'd.
        candidates = []
        data_dir = os.environ.get("TALA_CHAIN_DATA_DIR", "")
        if data_dir:
            candidates.append(Path(data_dir).resolve())
        user_data = os.environ.get("TALA_USER_DATA", "")
        if user_data:
            candidates.append(Path(user_data).resolve() / "blockchain" / "data")
        candidates.append(Path(__file__).resolve().parents[2] / "blockchain" / "data")
        for base in candidates:
            try:
                secret = (base / "admin_secret.txt").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if secret:
                break
    return {"X-Tala-Chain-Secret": secret} if secret else {}

def _get_remote_status() -> dict[str, Any]:
    """
    NAME
           get_remote_status - status of the configured shared chain

    SYNOPSIS
           status = _get_remote_status()

    DESCRIPTION
           Reads TALA_REAL_CHAIN_ENDPOINT, falling back to nothing. This used
           to hardcode https://talacore.app, which made a single external
           domain an implicit dependency of a portable, offline-first install
           -- and the blocks it returned were fed into the local chain.

           An install with no shared chain configured is not in an error state,
           so the absence of an endpoint is reported as such rather than as a
           failure.
    """
    endpoint = os.environ.get("TALA_REAL_CHAIN_ENDPOINT", "").strip()
    if not endpoint:
        return {"error": "no shared chain endpoint is configured"}
    try:
        return _get_json(f"{endpoint.rstrip('/')}/chain/status")
    except Exception as exc:
        return {"error": str(exc)}


async def pre_flight_reconciliation() -> bool:
    """
    NAME
           pre_flight_reconciliation - bring the shared chain up to date before a run

    SYNOPSIS
           success = await pre_flight_reconciliation()

    DESCRIPTION
           Reconciles the *shared* chain, and leaves the local Ghost chain
           alone.

           This used to compare the local Ghost chain's height against the
           remote chain's and, when the local one was shorter, fetch the remote
           blocks and POST them into the local node -- which under the old
           overwrite rule replaced the entire local history with a remote one
           (UI-124). It ran on every goal execution. Only the remote chain
           being unreachable kept it from firing.

           The premise was wrong as well as the mechanism. Ghost and Real are
           two chains split by audience, not two copies of one chain: Ghost
           holds everything this machine did and Real holds only what involved
           somebody else, so Ghost is *expected* to be taller and the
           difference between them is the design rather than a conflict to
           resolve. The else-branch of the old code half-recognised this and
           declined to push; the pull direction was never reconsidered.

           So there is nothing here to reconcile between them. What genuinely
           needs reconciling is the shared chain against its own index, and
           chains.reconcile() does that: it pulls, verifies what it pulled,
           writes only Real rows, and cannot touch Ghost.

    RETURN VALUE
           True when the local chain is reachable and any shared-chain
           reconcile either succeeded or was not applicable. False when the
           local chain cannot be reached, since that is what a caller checking
           this before a run wants to know.
    """
    import asyncio

    from python.harness.db import log_to_db

    log.info("GhostPersistence: pre-flight check.")

    local_status = await asyncio.to_thread(_get_local_status)
    if "error" in local_status:
        log.warning("GhostPersistence: local chain unreachable: %s",
                    local_status["error"])
        return False

    log.info("GhostPersistence: local Ghost chain at height %s.",
             local_status.get("height"))

    # The shared chain, if one is configured. An install that never goes online
    # has no endpoint and nothing to reconcile, which is an ordinary state and
    # not a failure.
    try:
        from python.harness import chains

        if not await chains.real_endpoint():
            log.info("GhostPersistence: no shared chain configured; "
                     "nothing to reconcile.")
            return True

        result = await chains.reconcile()
        if not result.get("reconciled"):
            log.warning("GhostPersistence: could not reconcile the shared "
                        "chain: %s", result.get("reason"))
            try:
                await log_to_db(
                    level="WARNING",
                    message=(f"Shared chain reconcile failed: "
                             f"{result.get('reason', 'unknown')}"),
                    trace={"event_type": "reconcile-failed", "result": result},
                )
            except Exception as db_exc:
                log.warning("GhostPersistence: could not log that: %s", db_exc)
            # The local chain is fine and the run can proceed; only the shared
            # chain is out of date.
            return True

        if result.get("learned") or result.get("confirmed"):
            log.info("GhostPersistence: reconciled the shared chain — "
                     "%s learned from peers, %s of ours confirmed.",
                     result.get("learned"), result.get("confirmed"))
        return True
    except Exception as exc:
        log.error("GhostPersistence: shared chain reconcile failed: %s", exc)
        return True


def check_hash() -> str:
    """
    NAME
           check_hash - a cheap health check on the local chain

    SYNOPSIS
           divergence_type = check_hash()

    DESCRIPTION
           Returns:
           - 'none'  the local chain is reachable and reports itself valid.
           - 'minor' the local chain is unreachable. Nothing is wrong with it
                     as far as anybody can tell; it simply cannot be asked.
           - 'major' the local chain is reachable and does *not* verify. That
                     is the one condition worth pausing a task for.

           This used to compare the local chain's height and latest hash
           against the remote chain's and report any difference as 'major'.
           That premise does not survive the Ghost/Real split: they are two
           chains divided by audience, not two copies of one chain. Ghost holds
           everything this machine did, Real holds only what involved somebody
           else, so they differ permanently and by design. Every goal run would
           have reported a major divergence, blocked the task and triggered a
           sync -- and the sync it triggered was the one that overwrote the
           local chain (UI-124).

           Reachability of the shared chain is not this function's business.
           An install that never goes online is not diverged.
    """
    local_status = _get_local_status()

    if "error" in local_status:
        return "minor"
    if local_status.get("isValid") is False:
        return "major"
    return "none"

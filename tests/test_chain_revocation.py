"""
Retiring a key, and how that reaches everybody else.

Withdrawing a signer locally told nobody. Revocation fixes that — but only one
kind of revocation can travel safely.

A revocation **signed by the key it retires** is a fact anyone can check:
nobody but the holder could have produced it, so every node acts on it without
needing to trust whoever relayed it. *"I no longer trust your key"* is an
opinion, and an opinion on a shared chain would let any signer evict any other.
So self-revocation goes on the chain and propagates; local distrust stays local.

Two properties do most of the work here:

* **Not retroactive.** Blocks signed before the revocation stay valid.
  Otherwise retiring a key would collapse the history it wrote and nobody could
  ever safely retire one.
* **Applied, not queued.** A revocation is acted on during reconcile rather
  than waiting for someone to approve it — waiting for a click is exactly the
  wrong behaviour for a key whose owner has declared it compromised.
"""
from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from python.harness import chain_verify, chains

HOLDER = Ed25519PrivateKey.generate()
HOLDER_PEM = HOLDER.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
HOLDER_PRINT = chain_verify.fingerprint(HOLDER_PEM)

REVOKED_AT = "2026-08-28T12:00:00.000Z"


def revocation(key=HOLDER, pem=HOLDER_PEM, sign_with=None, claim=None,
               revoked_at=REVOKED_AT):
    """A revocation transaction, forgeable in the ways the tests need."""
    print_ = claim or chain_verify.fingerprint(pem)
    message = chain_verify._revocation_message(print_, revoked_at)
    return {
        "id": f"revocation-{print_}",
        "type": "key-revocation",
        "hash": hashlib.sha256(message.encode()).hexdigest(),
        "creator": print_,
        "counterparty": "chain",
        "timestamp": 1756000020000,
        "document": None,
        "metadata": {"fingerprint": print_, "public_key": pem,
                     "revoked_at": revoked_at, "reason": ""},
        "signature": (sign_with or key).sign(message.encode()).hex(),
        "public_key": pem,
    }


def chain_with(*transaction_sets, key=HOLDER, pem=HOLDER_PEM):
    """A valid signed chain whose blocks carry the given transactions."""
    blocks = []
    previous = "0"
    for index, transactions in enumerate(transaction_sets):
        block = {"index": index, "previousHash": previous,
                 "timestamp": 1756000000000 + index * 1000,
                 "transactions": list(transactions), "nonce": 0}
        block["hash"] = chain_verify.block_hash(block)
        if index:
            block["signature"] = key.sign(block["hash"].encode()).hex()
            block["public_key"] = pem
        else:
            block["signature"] = None
            block["public_key"] = None
        previous = block["hash"]
        blocks.append(block)
    return blocks


def plain(tx_id):
    return {"id": tx_id, "type": "peer-skill", "hash": "0" * 64,
            "creator": "@peer", "counterparty": "@other",
            "timestamp": 1756000000000, "document": None, "metadata": None,
            "signature": None, "public_key": None}


# -- reading a revocation -----------------------------------------------------

def test_a_self_signed_revocation_is_accepted():
    assert chain_verify._verify_revocation(revocation()) == HOLDER_PRINT


def test_a_revocation_signed_by_somebody_else_is_ignored():
    """Otherwise anyone could retire anyone's key — a denial of service dressed
    up as a security feature."""
    attacker = Ed25519PrivateKey.generate()

    assert chain_verify._verify_revocation(revocation(sign_with=attacker)) == ""


def test_a_revocation_naming_one_key_and_carrying_another_is_ignored():
    """The fingerprint is derived, never believed: otherwise a revocation
    retires whichever key the reader happens to look at."""
    assert chain_verify._verify_revocation(
        revocation(claim="deadbeefdeadbeef")) == ""


def test_a_revocation_over_a_different_timestamp_is_ignored():
    """The signature covers the moment as well as the key, so a revocation
    cannot be lifted out and re-dated."""
    forged = revocation()
    forged["metadata"]["revoked_at"] = "2020-01-01T00:00:00.000Z"

    assert chain_verify._verify_revocation(forged) == ""


@pytest.mark.parametrize("missing", ["public_key", "fingerprint", "revoked_at"])
def test_an_incomplete_revocation_is_ignored(missing):
    broken = revocation()
    broken["metadata"][missing] = ""
    broken["public_key"] = "" if missing == "public_key" else broken["public_key"]

    assert chain_verify._verify_revocation(broken) == ""


def test_revocations_are_found_with_their_position():
    """The position matters: revocation is not retroactive, so *where* it
    happened decides which blocks it affects."""
    blocks = chain_with([plain("genesis")], [plain("t1")], [revocation()])

    assert chain_verify.revocations_in(blocks) == {HOLDER_PRINT: 2}


def test_the_earliest_revocation_wins():
    """A key is retired from the first moment it said so; a later repeat must
    not move the cutoff forward and re-validate blocks in between."""
    blocks = chain_with([plain("genesis")], [revocation()], [plain("t")],
                        [revocation(revoked_at="2026-09-01T00:00:00.000Z")])

    assert chain_verify.revocations_in(blocks)[HOLDER_PRINT] == 1


def test_the_message_matches_the_node_server():
    """Both sides must build it identically, or a valid revocation reads as a
    forged one on one of them and they disagree about which keys are usable."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "blockchain" / "src" / "app.js"
              ).read_text(encoding="utf-8")
    assert f"const REVOCATION_PREFIX = '{chain_verify.REVOCATION_PREFIX}'" in source
    assert "${REVOCATION_PREFIX}${fingerprint}:${revokedAt}" in source


def test_the_revocation_message_is_domain_separated():
    """Same trap as proof of possession: a signature over a bare 64-character
    hex string would be usable as a block signature.

    The property is that the message is not *hex*, not that it is a different
    length — with a 16-character fingerprint and an ISO timestamp this message
    happens to be exactly 64 characters long, which is a good reminder that
    length was never what made it safe.
    """
    message = chain_verify._revocation_message("a" * 16, REVOKED_AT)

    assert message.startswith(chain_verify.REVOCATION_PREFIX)
    assert not all(character in "0123456789abcdef" for character in message)


# -- what a revocation does to validity ---------------------------------------

def test_blocks_signed_before_retiring_stay_valid():
    """Otherwise retiring a key collapses the history it wrote, and nobody
    could ever safely retire one."""
    blocks = chain_with([plain("genesis")], [plain("t1")], [revocation()])

    assert chain_verify.verify(blocks, authorities={HOLDER_PRINT})


def test_a_block_signed_after_retiring_is_refused():
    blocks = chain_with([plain("genesis")], [revocation()], [plain("after")])

    verdict = chain_verify.verify(blocks, authorities={HOLDER_PRINT})

    assert not verdict.valid
    assert any("stopped being a permitted signer at block 1" in problem
               for problem in verdict.problems)


def test_a_revocation_is_honoured_without_an_authority_list():
    """A key that retired itself is not a matter of local policy: its holder
    said not to trust it, in a signature anyone can check."""
    blocks = chain_with([plain("genesis")], [revocation()], [plain("after")])

    assert not chain_verify.verify(blocks).valid


def test_a_forged_revocation_does_not_retire_anything():
    attacker = Ed25519PrivateKey.generate()
    blocks = chain_with([plain("genesis")], [revocation(sign_with=attacker)],
                        [plain("after")])

    assert chain_verify.verify(blocks, authorities={HOLDER_PRINT})


# -- propagation --------------------------------------------------------------

@pytest.fixture
def install(monkeypatch):
    """One install's authority list and offers record, in memory."""
    state = {"authorities": {HOLDER_PRINT}, "offers": {}, "logged": [],
             "stored": []}

    from python.harness import db

    async def execute(query, *args, caller=""):
        if "chain_key_offers" in query:
            state["offers"][args[0]] = "rejected"
        elif "app_settings" in query:
            import json as _json
            state["stored"] = _json.loads(args[0]).get("authorities", [])
        return "INSERT 0 1"

    async def fetchrow(query, *args):
        import json as _json
        return {"value": _json.dumps(
            {"authorities": [{"fingerprint": f, "until": None}
                             for f in state["authorities"]]})}

    async def log(level, message, trace=None, tx_id=None):
        state["logged"].append((level, message))

    async def authorities():
        return set(state["authorities"])

    async def set_authorities(fingerprints):
        state["authorities"] = set(fingerprints)
        return state["authorities"]

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "fetchrow", fetchrow)
    monkeypatch.setattr(db, "log_to_db", log)
    monkeypatch.setattr(chains, "authorities", authorities)
    monkeypatch.setattr(chains, "set_authorities", set_authorities)
    return state


@pytest.mark.asyncio
async def test_a_revocation_withdraws_the_signer_here(install):
    """The propagation. Nobody told this install anything; it read the chain."""
    blocks = chain_with([plain("genesis")], [revocation()])

    withdrawn = await chains._apply_revocations(blocks, {HOLDER_PRINT})

    assert withdrawn == {HOLDER_PRINT}
    # Stopped at the block it retired in, not struck from the list: erasing it
    # would refuse every block it signed while it *was* trusted.
    cutoff = {entry["fingerprint"]: entry["until"] for entry in install["stored"]}
    assert cutoff == {HOLDER_PRINT: 1}
    assert install["offers"][HOLDER_PRINT] == "rejected"
    assert install["logged"][0][0] == "WARNING"


@pytest.mark.asyncio
async def test_it_is_applied_rather_than_queued(install):
    """Waiting for someone to approve it is the wrong behaviour for a key whose
    owner has declared it compromised."""
    blocks = chain_with([plain("genesis")], [revocation()])

    await chains._apply_revocations(blocks, {HOLDER_PRINT})

    cutoff = {entry["fingerprint"]: entry["until"] for entry in install["stored"]}
    assert cutoff[HOLDER_PRINT] is not None, "acted on without waiting to be asked"


@pytest.mark.asyncio
async def test_other_signers_are_left_alone(install):
    blocks = chain_with([plain("genesis")], [revocation()])

    install["authorities"] = {HOLDER_PRINT, "aaaaaaaaaaaaaaaa"}

    await chains._apply_revocations(blocks, {HOLDER_PRINT, "aaaaaaaaaaaaaaaa"})

    cutoff = {entry["fingerprint"]: entry["until"] for entry in install["stored"]}
    assert cutoff[HOLDER_PRINT] == 1
    assert cutoff["aaaaaaaaaaaaaaaa"] is None, "an untouched signer keeps signing"


@pytest.mark.asyncio
async def test_a_revocation_for_a_key_we_never_trusted_is_still_recorded(install):
    """So a key that turns up in an offer later is already known to be
    retired."""
    blocks = chain_with([plain("genesis")], [revocation()])

    withdrawn = await chains._apply_revocations(blocks, set())

    assert withdrawn == set()
    assert install["offers"][HOLDER_PRINT] == "rejected"


@pytest.mark.asyncio
async def test_a_forged_revocation_withdraws_nothing(install):
    attacker = Ed25519PrivateKey.generate()
    blocks = chain_with([plain("genesis")], [revocation(sign_with=attacker)])

    withdrawn = await chains._apply_revocations(blocks, {HOLDER_PRINT})

    assert withdrawn == set()
    cutoff = {entry["fingerprint"]: entry["until"] for entry in install["stored"]}
    assert cutoff.get(HOLDER_PRINT) is None


@pytest.mark.asyncio
async def test_a_chain_with_no_revocations_costs_nothing(install):
    blocks = chain_with([plain("genesis")], [plain("t1")])

    assert await chains._apply_revocations(blocks, {HOLDER_PRINT}) == set()
    assert install["logged"] == []


# -- a retired key cannot be re-admitted --------------------------------------

@pytest.mark.asyncio
async def test_a_retired_key_cannot_be_trusted(monkeypatch):
    """Admitting it would produce an authority list nothing else on the chain
    would honour."""
    from python.harness import chain_keys, db

    async def fetchrow(query, *args):
        return {"fingerprint": HOLDER_PRINT, "proven": True, "label": "",
                "note": chain_keys.RETIRED_NOTE}
    monkeypatch.setattr(db, "fetchrow", fetchrow)

    result = await chain_keys.trust(HOLDER_PRINT)

    assert not result["trusted"]
    assert "retired" in result["reason"]


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    assert "chain.retire_key" in _HANDLERS
    assert "chain.retired_keys" in _HANDLERS


# -- withdrawal is a cutoff, not an erasure -----------------------------------
#
# Found by an end-to-end run against two real nodes, not by the unit tests.
# Membership of the authority list is consulted for *every* block, historical
# ones included, so dropping a signer refused everything it had ever written —
# the same "retiring collapses history" failure the revocation cutoff was
# designed to prevent, arriving through the other door. A revocation stopped
# the key at block R and the removal then refused it from block 1, leaving the
# chain permanently unverifiable.

def test_a_withdrawn_signer_keeps_its_history():
    blocks = chain_with([plain("genesis")], [plain("t1")], [plain("t2")])

    verdict = chain_verify.verify(blocks, authorities={HOLDER_PRINT},
                                  cutoffs={HOLDER_PRINT: 2})

    assert verdict.valid, verdict.problems


def test_a_withdrawn_signer_may_not_sign_after_the_cutoff():
    blocks = chain_with([plain("genesis")], [plain("t1")], [plain("t2")])

    verdict = chain_verify.verify(blocks, authorities={HOLDER_PRINT},
                                  cutoffs={HOLDER_PRINT: 1})

    assert not verdict.valid
    assert any("stopped being a permitted signer at block 1" in problem
               for problem in verdict.problems)


def test_striking_a_signer_out_entirely_would_refuse_its_history():
    """The behaviour being avoided, asserted so nobody restores it by
    'simplifying' the cutoff away."""
    blocks = chain_with([plain("genesis")], [plain("t1")])

    assert not chain_verify.verify(blocks, authorities=set()).valid
    assert chain_verify.verify(blocks, authorities={HOLDER_PRINT},
                               cutoffs={HOLDER_PRINT: 1}).valid


def test_a_chain_revocation_beats_a_later_local_cutoff():
    """Every other node will use the revocation's own position, so a local
    cutoff further along must not let this install accept blocks they refuse."""
    blocks = chain_with([plain("genesis")], [revocation()], [plain("after")])

    verdict = chain_verify.verify(blocks, authorities={HOLDER_PRINT},
                                  cutoffs={HOLDER_PRINT: 99})

    assert not verdict.valid


@pytest.mark.asyncio
async def test_withdrawing_records_a_cutoff_rather_than_forgetting(monkeypatch):
    from python.harness import db

    stored = {"value": '{"authorities": [{"fingerprint": "%s", "until": null}]}'
                       % HOLDER_PRINT}
    written = {}

    async def fetchrow(query, *args):
        return stored

    async def fetchval(query, *args):
        return 7

    async def execute(query, *args, caller=""):
        import json
        written.update(json.loads(args[0]))
        return "INSERT 0 1"

    monkeypatch.setattr(db, "fetchrow", fetchrow)
    monkeypatch.setattr(db, "fetchval", fetchval)
    monkeypatch.setattr(db, "execute", execute)

    await chains.set_authorities([])

    assert written["authorities"] == [{"fingerprint": HOLDER_PRINT, "until": 7}]


@pytest.mark.asyncio
async def test_re_admitting_a_withdrawn_signer_clears_its_cutoff(monkeypatch):
    from python.harness import db

    stored = {"value": '{"authorities": [{"fingerprint": "%s", "until": 7}]}'
                       % HOLDER_PRINT}
    written = {}

    async def fetchrow(query, *args):
        return stored

    async def fetchval(query, *args):
        return 9

    async def execute(query, *args, caller=""):
        import json
        written.update(json.loads(args[0]))
        return "INSERT 0 1"

    monkeypatch.setattr(db, "fetchrow", fetchrow)
    monkeypatch.setattr(db, "fetchval", fetchval)
    monkeypatch.setattr(db, "execute", execute)

    await chains.set_authorities([HOLDER_PRINT])

    assert written["authorities"] == [{"fingerprint": HOLDER_PRINT, "until": None}]

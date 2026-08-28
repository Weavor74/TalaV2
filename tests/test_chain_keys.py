"""
Obtaining a peer's signing key, and deciding whether to trust it.

The hard part of key distribution is not fetching. It is knowing the key is
theirs. There is no certificate authority here, no web of trust, and no channel
this system controls that an attacker in the middle would not also control —
so anything that fetches a key and admits it is not distribution, it is the
signature-trusting defect moved one layer out.

Fetching and trusting are therefore separate acts, and these tests exist mostly
to hold them apart.
"""
from __future__ import annotations

import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from python.harness import chain_keys, chain_verify

PEER_KEY = Ed25519PrivateKey.generate()
PEER_PEM = PEER_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
PEER_PRINT = chain_verify.fingerprint(PEER_PEM)


@pytest.fixture
def peer(monkeypatch):
    """A peer node to fetch from, and an in-memory record of what was stored."""
    state = {
        "public_key": PEER_PEM,
        "identity_status": 200,
        "prove_status": 200,
        "sign_with": PEER_KEY,
        "advertise": PEER_PEM,
        "echo_nonce": True,
    }
    recorded: list[dict] = []

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return Response(state["identity_status"], {
                "public_key": state["advertise"],
                "fingerprint": chain_verify.fingerprint(state["advertise"]),
            })

        async def post(self, url, json=None):
            nonce = (json or {}).get("nonce", "")
            echoed = nonce if state["echo_nonce"] else secrets.token_hex(32)
            message = f"{chain_keys.POP_PREFIX}{echoed}"
            return Response(state["prove_status"], {
                "nonce": echoed,
                "prefix": chain_keys.POP_PREFIX,
                "public_key": state["public_key"],
                "signature": state["sign_with"].sign(message.encode()).hex(),
            })

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def net_check(target, principal, purpose=""):
        from python.harness import network_guard as ng
        return ng.NetDecision(True, "permitted", "peer.example", 443, "", {})
    from python.harness import network_guard as ng
    monkeypatch.setattr(ng, "check", net_check)

    async def record(fingerprint, public_key, label, source, proven, note):
        recorded.append({"fingerprint": fingerprint, "public_key": public_key,
                         "label": label, "source": source, "proven": proven,
                         "note": note})
    monkeypatch.setattr(chain_keys, "_record", record)

    return {"state": state, "recorded": recorded}


# -- fetching -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_key_is_fetched_and_possession_proved(peer):
    result = await chain_keys.fetch("https://peer.example", label="Alice")

    assert result["fetched"]
    assert result["fingerprint"] == PEER_PRINT
    assert result["proven"] is True
    assert result["status"] == chain_keys.OFFERED


@pytest.mark.asyncio
async def test_fetching_never_trusts(peer, monkeypatch):
    """The whole point of the split. A key that arrived over the network is a
    claim, not a decision."""
    from python.harness import chains

    touched = []

    async def set_authorities(fingerprints):
        touched.append(fingerprints)
        return set(fingerprints)
    monkeypatch.setattr(chains, "set_authorities", set_authorities)

    await chain_keys.fetch("https://peer.example")

    assert touched == [], "fetching admitted a signer"


@pytest.mark.asyncio
async def test_a_peer_that_cannot_prove_possession_is_recorded_as_such(peer):
    """Without this a directory could advertise somebody else's key and have
    it admitted on their behalf."""
    peer["state"]["prove_status"] = 500

    result = await chain_keys.fetch("https://peer.example")

    assert result["fetched"]          # the key is still recorded
    assert result["proven"] is False  # but the failure is not hidden
    assert "prove possession" in result["note"]


@pytest.mark.asyncio
async def test_signing_with_a_different_key_than_advertised_is_caught(peer):
    impostor = Ed25519PrivateKey.generate()
    peer["state"]["sign_with"] = impostor
    peer["state"]["public_key"] = impostor.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

    result = await chain_keys.fetch("https://peer.example")

    assert result["proven"] is False
    assert "different key than it advertised" in result["note"]


@pytest.mark.asyncio
async def test_a_signature_over_the_wrong_challenge_is_caught(peer):
    """A proof answering a nonce we did not choose is a replayed proof, and
    says nothing about who is answering now."""
    peer["state"]["echo_nonce"] = False

    result = await chain_keys.fetch("https://peer.example")

    assert result["proven"] is False
    assert "different challenge" in result["note"]


@pytest.mark.asyncio
async def test_a_forged_signature_is_caught(peer):
    stranger = Ed25519PrivateKey.generate()
    peer["state"]["sign_with"] = stranger   # advertises PEER_PEM, signs with another

    result = await chain_keys.fetch("https://peer.example")

    assert result["proven"] is False
    assert "did not verify" in result["note"]


@pytest.mark.asyncio
async def test_the_advertised_fingerprint_is_recomputed_not_believed(peer):
    """A node that misreports its own fingerprint would otherwise get an
    out-of-band comparison to succeed against a key it will not sign with."""
    other = Ed25519PrivateKey.generate()
    other_pem = other.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    peer["state"]["advertise"] = other_pem
    peer["state"]["public_key"] = other_pem
    peer["state"]["sign_with"] = other

    result = await chain_keys.fetch("https://peer.example")

    assert result["fingerprint"] == chain_verify.fingerprint(other_pem)
    assert result["fingerprint"] != PEER_PRINT


@pytest.mark.asyncio
async def test_a_peer_advertising_no_key_is_reported(peer):
    peer["state"]["advertise"] = ""

    result = await chain_keys.fetch("https://peer.example")

    assert not result["fetched"]
    assert "no signing key" in result["reason"]


@pytest.mark.asyncio
async def test_the_guardrails_govern_key_exchange(peer, monkeypatch):
    from python.harness import network_guard as ng

    async def refuse(target, principal, purpose=""):
        return ng.NetDecision(False, "blocked by rule", "peer.example", 443,
                              "deny:*", {})
    monkeypatch.setattr(ng, "check", refuse)

    result = await chain_keys.fetch("https://peer.example")

    assert not result["fetched"]
    assert "blocked by rule" in result["reason"]
    assert peer["recorded"] == []


@pytest.mark.asyncio
async def test_a_guard_that_cannot_be_consulted_does_not_permit_the_fetch(
        peer, monkeypatch):
    from python.harness import network_guard as ng

    async def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(ng, "check", boom)

    result = await chain_keys.fetch("https://peer.example")

    assert not result["fetched"]
    assert "guardrails unavailable" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["", "   ", "peer.example", "ftp://peer"])
async def test_an_endpoint_that_is_not_one_is_refused(peer, endpoint):
    result = await chain_keys.fetch(endpoint)
    assert not result["fetched"]


# -- keys handed over directly ------------------------------------------------

@pytest.mark.asyncio
async def test_a_pasted_key_is_recorded_without_a_proof(peer):
    """A key read off a screen has no proof of possession attached, and is
    recorded as such rather than quietly treated as though it had one."""
    result = await chain_keys.offer(PEER_PEM, label="Bob")

    assert result["offered"]
    assert result["proven"] is False
    assert peer["recorded"][0]["note"] == "supplied directly; possession not tested"


@pytest.mark.asyncio
async def test_something_that_is_not_a_key_is_refused(peer):
    result = await chain_keys.offer("not a key at all")

    assert not result["offered"]
    assert "not a usable public key" in result["reason"]
    assert peer["recorded"] == []


# -- deciding -----------------------------------------------------------------

@pytest.fixture
def decisions(monkeypatch):
    """The offers table and authority list, in memory."""
    state = {"offers": {}, "authorities": set(), "logged": []}

    from python.harness import chains, db

    async def fetchrow(query, *args):
        row = state["offers"].get(args[0]) if args else None
        # trust() reads `note` to spot a key its holder has retired.
        return {**row, "note": row.get("note", "")} if row else None

    async def execute(query, *args, caller=""):
        if "UPDATE chain_key_offers" in query:
            row = state["offers"].get(args[0])
            if row:
                row["status"] = "trusted" if "'trusted'" in query else "rejected"
        return "UPDATE 1"

    async def log(level, message, trace=None, tx_id=None):
        state["logged"].append((level, message))

    async def authorities():
        return set(state["authorities"])

    async def set_authorities(fingerprints):
        state["authorities"] = set(fingerprints)
        return state["authorities"]

    monkeypatch.setattr(db, "fetchrow", fetchrow)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "log_to_db", log)
    monkeypatch.setattr(chains, "authorities", authorities)
    monkeypatch.setattr(chains, "set_authorities", set_authorities)
    return state


@pytest.mark.asyncio
async def test_trusting_an_offered_key_admits_it(decisions):
    decisions["offers"][PEER_PRINT] = {"fingerprint": PEER_PRINT, "proven": True,
                                       "label": "Alice", "status": "offered"}

    result = await chain_keys.trust(PEER_PRINT)

    assert result["trusted"]
    assert PEER_PRINT in decisions["authorities"]
    assert decisions["offers"][PEER_PRINT]["status"] == "trusted"


@pytest.mark.asyncio
async def test_a_key_nobody_offered_cannot_be_trusted(decisions):
    """Trusting a key nobody has seen is not a thing to make convenient."""
    result = await chain_keys.trust("0123456789abcdef")

    assert not result["trusted"]
    assert "not been offered" in result["reason"]
    assert decisions["authorities"] == set()


@pytest.mark.asyncio
async def test_trusting_keeps_the_signers_already_permitted(decisions):
    decisions["authorities"] = {"aaaaaaaaaaaaaaaa"}
    decisions["offers"][PEER_PRINT] = {"fingerprint": PEER_PRINT, "proven": True,
                                       "label": "", "status": "offered"}

    await chain_keys.trust(PEER_PRINT)

    assert decisions["authorities"] == {"aaaaaaaaaaaaaaaa", PEER_PRINT}


@pytest.mark.asyncio
async def test_an_unproven_key_can_still_be_trusted_but_it_is_recorded(decisions):
    """Possession is evidence, not a gate: a key handed over in person has no
    network proof and is perfectly trustworthy. What matters is that the
    absence is written down rather than glossed over."""
    decisions["offers"][PEER_PRINT] = {"fingerprint": PEER_PRINT, "proven": False,
                                       "label": "in person", "status": "offered"}

    result = await chain_keys.trust(PEER_PRINT)

    assert result["trusted"]
    assert decisions["logged"], "the decision was recorded"


@pytest.mark.asyncio
async def test_rejecting_withdraws_a_trusted_key(decisions):
    decisions["authorities"] = {PEER_PRINT}
    decisions["offers"][PEER_PRINT] = {"fingerprint": PEER_PRINT, "proven": True,
                                       "label": "", "status": "trusted"}

    result = await chain_keys.reject(PEER_PRINT)

    assert result["rejected"]
    assert result["was_trusted"]
    assert PEER_PRINT not in decisions["authorities"]
    # withdrawal is not retroactive, and says so
    assert "stay on the chain" in result["note"]
    assert decisions["logged"][0][0] == "WARNING"


@pytest.mark.asyncio
async def test_rejecting_a_key_that_was_never_trusted_is_quiet(decisions):
    result = await chain_keys.reject("0123456789abcdef")

    assert result["rejected"]
    assert not result["was_trusted"]
    assert decisions["logged"] == []


# -- the contract with the Node server ----------------------------------------

def test_the_proof_prefix_matches_the_node_server():
    """Domain separation only works if both sides use the same domain."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "blockchain" / "src" / "app.js"
              ).read_text(encoding="utf-8")
    assert f"const POP_PREFIX = '{chain_keys.POP_PREFIX}'" in source


def test_the_proof_is_domain_separated():
    """Block signatures are made over a bare 64-character hex hash. A signing
    oracle over unprefixed input would hand anyone a valid block signature for
    a block of their choosing, so the prefix is the whole reason the proof
    endpoint is safe to expose."""
    assert chain_keys.POP_PREFIX
    assert not chain_keys.POP_PREFIX[0].isdigit()
    # a prefixed message can never be mistaken for a bare hash
    message = f"{chain_keys.POP_PREFIX}{'a' * 64}"
    assert len(message) != 64
    assert not all(character in "0123456789abcdef" for character in message)


def test_the_rpcs_exist():
    from python.harness.server import _HANDLERS

    for method in ("chain.identity", "chain.fetch_key", "chain.offer_key",
                   "chain.key_offers", "chain.trust_key", "chain.reject_key"):
        assert method in _HANDLERS

"""
Who a request is billed to.

inference.generate picked the "resource owner" with `re.search(r"@([a-zA-Z0-9_-]+)", prompt)`: the first `@word` anywhere in the
prompt, system context and tool output included. In `vps` mode a named peer other than yourself is paid in real tokens, so a
decorator (`@register_tool`), an email address (`name@example.com`) or a handle in a file the model had just read turned a local
call into a peer payment, and a wallet with no balance refused it: "Insufficient token balance" in 0.0 s, on the user's own
model. Only a registered contact is a peer.
"""
from __future__ import annotations

import asyncio

from python.harness import peer_billing


def run(coro):
    return asyncio.run(coro)


class Contacts:
    def __init__(self, *names):
        self.names = set(names)
        self.asked = []

    async def __call__(self, username=None, wallet_address=None):
        self.asked.append(username)
        return {"username": username} if username in self.names else None


def test_a_prompt_with_no_handle_is_nobody_elses():
    assert run(peer_billing.known_peer("read generate_stories.py please", Contacts("alice"))) is None


def test_a_decorator_is_not_a_peer():
    prompt = "class Tool:\n    @register_tool\n    def run(self): ...\n@dataclass\nclass X: ..."
    assert run(peer_billing.known_peer(prompt, Contacts("alice"))) is None


def test_an_email_address_is_not_a_peer():
    assert run(peer_billing.known_peer("write to steven@example.com about it", Contacts("alice"))) is None


def test_a_registered_contact_is_the_peer():
    assert run(peer_billing.known_peer("ask @alice what she thinks", Contacts("alice"))) == "@alice"


def test_the_first_known_contact_wins_over_earlier_unknown_handles():
    prompt = "@register_tool then @bob then @alice"
    assert run(peer_billing.known_peer(prompt, Contacts("alice", "bob"))) == "@bob"


def test_each_handle_is_looked_up_once_and_only_so_many_are_tried():
    prompt = " ".join(f"@h{i}" for i in range(40)) + " @h0 @h0"
    contacts = Contacts()
    assert run(peer_billing.known_peer(prompt, contacts)) is None
    assert len(contacts.asked) == len(set(contacts.asked)) == peer_billing.MAX_LOOKUPS


def test_a_lookup_that_fails_bills_nobody():
    async def broken(username=None, wallet_address=None):
        raise RuntimeError("database is down")

    assert run(peer_billing.known_peer("ask @alice", broken)) is None


def test_the_chat_handler_resolves_its_peer_through_this_module():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "python" / "harness" / "server.py").read_text(encoding="utf-8")
    assert "primary_peer = await _peer_billing.known_peer(" in src
    assert "peer_match = re.search(" not in src

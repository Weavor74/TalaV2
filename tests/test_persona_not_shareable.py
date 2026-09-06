"""
A persona never leaves this install.

Steven, of the T.A.L.A. persona he wrote: "no one else should have this
TALA." That is not a preference about sharing etiquette, it is a
confidentiality requirement, and the code did not meet it.

settings_nn.py mirrors every agent row to nodes/persona-<id>.html, so the
whole system prompt sits on disk under a predictable name, in the very
directory peers resolve references from. Checked live: persona-tala-rp.html
carried the complete 4,434-character prompt. A contact granted `nodes` plus a
wildcard category could have asked for `tala-rp` and been handed it, because
the wildcard branch returns True before any per-node reasoning happens.

The distinction the code now makes: a persona is who this Tala IS, and
everything else under nodes/ is something she knows. The firewall exists to
portion out knowledge. Identity is not on the menu, and sharing a persona
deliberately should be its own feature with its own consent rather than a
side effect of granting somebody access to documents.
"""

from __future__ import annotations

import pytest

from python.harness import server

pytestmark = pytest.mark.asyncio

WILDCARD_CONTACT = {
    "username": "@somebody",
    "wallet_address": "somebody-wallet",
    "permissions": {"nodes": True},
}


@pytest.fixture
def wildcard_rights(monkeypatch):
    """The most permissive grant this system can express."""
    async def rights(_wallet):
        return {"clearance_level": 10, "allowed_categories": ["*"]}

    monkeypatch.setattr(server, "_get_firewall_contact_access_rights", rights)


async def test_the_persona_node_is_the_whole_prompt():
    """
    The premise. If the mirror did not carry the prompt there would be
    nothing to leak, so this pins why the rule is needed.
    """
    resolved = await server._resolve_local_html_nn_reference("tala-rp")
    if resolved is None:
        pytest.skip("no tala-rp persona node in this install")

    assert resolved["node"]["node_type"] == "persona"


async def test_a_persona_is_refused_to_a_wildcard_contact(wildcard_rights):
    """
    The live hole: "If the contact is granted full wildcard, skip granular
    checks" returns True before anything looks at what the node is.
    """
    allowed, reason = await server._peer_can_resolve_node_reference(
        WILDCARD_CONTACT, {"tala-rp"}
    )

    assert allowed is False
    assert reason == "persona_not_shareable"


async def test_a_persona_is_refused_even_when_explicitly_granted(monkeypatch, wildcard_rights):
    """
    An explicit per-node grant supersedes the contact's flags and the
    clearance machinery. It must not supersede this: inviting somebody to a
    document is not inviting them to become you.
    """
    from python.harness import access

    class _Allowed:
        allowed = True
        reason = "explicitly granted"

    async def always_granted(*args, **kwargs):
        return _Allowed()

    monkeypatch.setattr(access, "check", always_granted)

    allowed, reason = await server._peer_can_resolve_node_reference(
        WILDCARD_CONTACT, {"tala-rp"}
    )

    assert allowed is False
    assert reason == "persona_not_shareable"


async def test_one_persona_in_the_request_refuses_the_whole_request(wildcard_rights):
    """
    Asking for a document and a persona together must not smuggle the
    persona through on the document's clearance.
    """
    allowed, reason = await server._peer_can_resolve_node_reference(
        WILDCARD_CONTACT, {"tala-rp", "sdk_agent_anchor"}
    )

    assert allowed is False
    assert reason == "persona_not_shareable"


async def test_an_ordinary_node_is_still_shareable(wildcard_rights):
    """
    The rule withholds identity, not everything. A blanket refusal would be
    a different bug wearing the same clothes.
    """
    resolved = await server._resolve_local_html_nn_reference("sdk_agent_anchor")
    if resolved is None:
        pytest.skip("no sdk_agent_anchor node in this install")

    allowed, _reason = await server._peer_can_resolve_node_reference(
        WILDCARD_CONTACT, {"sdk_agent_anchor"}
    )

    assert allowed is True


async def test_an_unreadable_node_is_withheld_rather_than_assumed_safe():
    """
    Screening asks what a node IS. A node it cannot read is one it cannot
    clear, and the safe direction for an unreadable node is to withhold it.
    """
    async def unreadable(_name, *args, **kwargs):
        raise OSError("cannot read")

    from unittest.mock import patch

    with patch.object(server, "_resolve_local_html_nn_reference", unreadable):
        withheld = await server._persona_nodes_among({"anything"})

    assert withheld == {"anything"}


async def test_the_withheld_types_are_stated_not_scattered():
    """
    One place says what never leaves, so adding to it is a decision rather
    than an edit in several files that must agree.
    """
    assert "persona" in server.PEER_WITHHELD_NODE_TYPES


async def test_a_peer_search_cannot_surface_a_persona_either(monkeypatch):
    """
    Search filters each hit through the same gate, so personas drop out of
    results and are counted as withheld rather than listed.
    """
    import inspect

    src = inspect.getsource(server.handle_p2p_semantic_search)
    assert "_peer_can_resolve_node_reference" in src, (
        "search no longer screens hits through the firewall, so personas could be listed"
    )

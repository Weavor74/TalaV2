"""
The user profile: name injected, everything else looked up, PII never.

Steven's design, in his words:

    "You cant inject the profile cluster in its entirety. You can inject the
    profile name and DOB but all other stuff should be looked up. The DOB is
    only for astrology comparison."

and, on the source of truth:

    "Is that tool connected to the html-nn of my profile?"

It is now. The node is read first and the database is the fallback, matching
the UI-099 loader fix -- the node is exactly the copy that was once found stale
and legacy-flat, so trusting it *alone* is what made the data look lost.
"""
from __future__ import annotations

import json

import pytest

from python.harness import user_profile as up

CONFIG = {
    "identity": {"name": "Steve", "nickname": "Weavor", "pronouns": "He/Him",
                 "location": "Texas", "avatar_url": "http://x/y.png"},
    "about": {"bio": "a biography"},
    "skills": [{"name": "AI Systems Evaluation"}],
    "connections": [{"name": "Someone", "trust_level": 10}],
    "preferences": {"tone": "Professional"},
    "pii_vault": {"legal_name": "REDACT ME", "dob": "1974-09-19",
                  "email": "a@b.c", "phone": "5551234"},
    "privacy": {"identity": "public", "about": "public", "skills": "public",
                "connections": "connections"},
}


@pytest.fixture
def cfg(monkeypatch):
    async def _load():
        return json.loads(json.dumps(CONFIG))
    monkeypatch.setattr(up, "load_config", _load)


# -- what reaches the prompt --------------------------------------------------

@pytest.mark.asyncio
async def test_only_the_name_is_injected(cfg):
    line = await up.system_prompt_line()

    assert "Weavor" in line
    # None of the rest of the profile may ride along.
    for leak in ("biography", "AI Systems Evaluation", "Texas", "Professional"):
        assert leak not in line, f"{leak!r} was injected instead of looked up"


@pytest.mark.asyncio
async def test_the_date_of_birth_is_never_in_the_prompt(cfg):
    """DOB feeds astrology, which reads the database directly. It is not
    persona colour and must not appear in prompt text."""
    line = await up.system_prompt_line()
    assert "1974" not in line
    assert "09-19" not in line


@pytest.mark.asyncio
async def test_no_pii_reaches_the_prompt(cfg):
    line = await up.system_prompt_line()
    for secret in ("REDACT ME", "a@b.c", "5551234"):
        assert secret not in line


@pytest.mark.asyncio
async def test_the_prompt_line_tells_the_model_the_lookup_exists(cfg):
    """Without this the model cannot know the capability is there, and will
    either guess or ask the user to repeat themselves."""
    line = await up.system_prompt_line()
    assert "user_profile_lookup" in line


@pytest.mark.asyncio
async def test_no_name_means_no_line(monkeypatch):
    async def _empty():
        return {}
    monkeypatch.setattr(up, "load_config", _empty)
    assert await up.system_prompt_line() == ""


def test_a_nickname_wins_over_the_formal_name():
    """Someone who sets a nickname is saying what to call them."""
    assert up.name_from(CONFIG) == "Weavor"


def test_the_legal_name_is_never_the_display_name():
    assert up.name_from({"pii_vault": {"legal_name": "REDACT ME"}}) == ""


# -- what a lookup will and will not return -----------------------------------

@pytest.mark.asyncio
async def test_a_public_section_is_returned(cfg):
    r = await up.lookup("skills")
    assert r["allowed"] is True
    assert r["data"][0]["name"] == "AI Systems Evaluation"


@pytest.mark.asyncio
async def test_pii_is_refused_at_every_visibility(cfg):
    r = await up.lookup("pii_vault")
    assert r["allowed"] is False
    assert r["data"] is None
    assert "REDACT ME" not in json.dumps(r)


@pytest.mark.asyncio
async def test_the_privacy_settings_are_not_readable_as_content(cfg):
    r = await up.lookup("privacy")
    assert r["allowed"] is False
    # and the refusal says why accurately -- privacy is config, not PII
    assert "access-control" in r["reason"]


@pytest.mark.asyncio
async def test_connections_only_means_not_this_agent(cfg):
    """Steven's model is invite-only: 'I have to invite her to access'."""
    r = await up.lookup("connections")
    assert r["allowed"] is False
    assert r["visibility"] == "connections"
    assert "Someone" not in json.dumps(r)


@pytest.mark.asyncio
async def test_a_section_with_no_visibility_set_is_withheld(cfg):
    """Fail closed: adding a profile section must not silently publish it."""
    r = await up.lookup("preferences")
    assert r["allowed"] is False
    assert "Professional" not in json.dumps(r)


@pytest.mark.asyncio
async def test_identity_is_reduced_to_an_allowlist(cfg):
    r = await up.lookup("identity")
    assert r["allowed"] is True
    assert r["data"]["nickname"] == "Weavor"
    assert "avatar_url" not in r["data"], "identity is an allowlist, not a blocklist"


@pytest.mark.asyncio
async def test_an_unknown_section_names_the_real_ones(cfg):
    r = await up.lookup("salary")
    assert r["allowed"] is False
    assert "skills" in r["reason"]


@pytest.mark.asyncio
async def test_withheld_and_empty_are_distinguishable(monkeypatch):
    """'They recorded no skills' and 'you may not read their skills' are
    different facts and must not look alike to the caller."""
    async def _load():
        return {"skills": [], "privacy": {"skills": "public"}}
    monkeypatch.setattr(up, "load_config", _load)

    empty = await up.lookup("skills")
    assert empty["allowed"] is True and empty["data"] == []

    async def _private():
        return {"skills": [{"name": "x"}], "privacy": {"skills": "private"}}
    monkeypatch.setattr(up, "load_config", _private)

    withheld = await up.lookup("skills")
    assert withheld["allowed"] is False


# -- the source of truth ------------------------------------------------------

def test_the_node_is_the_source(tmp_path, monkeypatch):
    """It must read the HTML-NN, not the database, when the node is usable."""
    import sdk.agent.documentation_nodes as dn

    node = tmp_path / "concept-user-profile.html"
    node.write_text(
        '<html><body><article data-node-id="user-profile" '
        'data-node-type="concept" data-execution-authority="0.0">'
        '<section id="semantic-memory">the user profile</section>'
        '<script id="node-state" type="application/json">'
        + json.dumps({"config": {"skills": [{"name": "from-the-node"}],
                                 "privacy": {"skills": "public"}}})
        + "</script></article></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(dn, "resolve_active_nodes_dir", lambda: tmp_path)

    config = up.load_config_from_node()
    assert config["skills"][0]["name"] == "from-the-node"


def test_a_legacy_flat_node_falls_back_rather_than_serving_half_a_profile(
    tmp_path, monkeypatch
):
    """UI-099: the live node was flat, carrying no config. A reader that
    accepted that shape served a profile with most sections missing."""
    import sdk.agent.documentation_nodes as dn

    node = tmp_path / "concept-user-profile.html"
    node.write_text(
        '<html><body><article data-node-id="user-profile" '
        'data-node-type="concept" data-execution-authority="0.0">'
        '<section id="semantic-memory">the user profile</section>'
        '<script id="node-state" type="application/json">'
        '{"name": "Steve", "birthdate": "1974-09-19"}'
        "</script></article></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(dn, "resolve_active_nodes_dir", lambda: tmp_path)

    assert up.load_config_from_node() == {}, "a flat node must read as unusable"


def test_a_missing_node_is_empty_not_an_error(tmp_path, monkeypatch):
    import sdk.agent.documentation_nodes as dn
    monkeypatch.setattr(dn, "resolve_active_nodes_dir", lambda: tmp_path)
    assert up.load_config_from_node() == {}


def test_double_encoded_config_is_decoded():
    """profiles.config is double-encoded: one loads() yields a string again."""
    inner = {"skills": [{"name": "x"}]}
    assert up._decode(json.dumps(json.dumps(inner))) == inner
    assert up._decode(json.dumps(inner)) == inner
    assert up._decode(inner) == inner
    assert up._decode("not json at all") == {}

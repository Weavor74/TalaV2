"""
Provider credentials must not cross the RPC boundary (SEC-W0-02).

``inference_providers.config`` is AES-encrypted at rest (UI-080), and then
``inference.get_providers`` decrypted it and shipped the plaintext ``apiKey``
to the caller. The socket it crossed is 8765, which has no authentication, and
WebSocket handshakes are not subject to CORS -- so any web page the user
happened to visit could read every provider key in the app.

Redaction alone would have been a second bug: ``save_providers`` replaces the
whole table from what the client sends, so a client that is no longer shown a
key would have written "" over every one of them on the next save. Both halves
are asserted here, because either half alone is worse than neither.
"""

import json

import pytest

from python.harness.credential_crypto import (
    REDACTED_SENTINEL,
    config_has_secret,
    merge_preserved_secrets,
    redact_config,
    resolve_secret,
)


# -- redaction ---------------------------------------------------------------

def test_redact_config_withholds_secrets_but_keeps_identity():
    cfg = {
        "apiKey": "sk-live-abcdef",
        "model": "gemini-2.0-pro",
        "endpoint": "https://example.invalid",
        "username": "steve",
    }
    out = redact_config(cfg)
    assert out["apiKey"] == REDACTED_SENTINEL
    assert "sk-live-abcdef" not in json.dumps(out)
    # Identity fields stay readable, or the settings panel cannot render.
    assert out["model"] == "gemini-2.0-pro"
    assert out["endpoint"] == "https://example.invalid"
    assert out["username"] == "steve"
    assert cfg["apiKey"] == "sk-live-abcdef", "input must not be mutated"


def test_redact_config_leaves_pii_alone():
    """PII is encrypted at rest but must still render; redacting it would blank
    the user's own details every time they open the panel."""
    out = redact_config({"dob": "1985-04-12", "apiKey": "k"})
    assert out["dob"] == "1985-04-12"
    assert out["apiKey"] == REDACTED_SENTINEL


def test_redact_config_distinguishes_absent_from_withheld():
    assert redact_config({"apiKey": ""})["apiKey"] == ""
    assert config_has_secret({"apiKey": ""}) is False
    assert config_has_secret({"apiKey": "k"}) is True
    assert config_has_secret({"model": "x"}) is False


# -- the write half ----------------------------------------------------------

@pytest.mark.parametrize("incoming,expected", [
    ({"apiKey": REDACTED_SENTINEL}, "stored-key"),   # never shown it
    ({},                            "stored-key"),   # did not mention it
    ({"apiKey": ""},                "stored-key"),   # blank box means unchanged
    ({"apiKey": "   "},             "stored-key"),
    ({"apiKey": "brand-new"},       "brand-new"),    # user typed one
])
def test_merge_preserved_secrets(incoming, expected):
    out = merge_preserved_secrets(dict(incoming), {"apiKey": "stored-key"})
    assert out["apiKey"] == expected


def test_merge_preserved_secrets_clears_only_on_explicit_null():
    out = merge_preserved_secrets({"apiKey": None}, {"apiKey": "stored-key"})
    assert "apiKey" not in out


@pytest.mark.parametrize("incoming,expected", [
    (REDACTED_SENTINEL, "stored"),
    ("",                "stored"),
    ("new",             "new"),
    (None,              ""),
])
def test_resolve_secret(incoming, expected):
    assert resolve_secret(incoming, "stored") == expected


# -- the handler ------------------------------------------------------------

@pytest.mark.asyncio
async def test_inference_get_providers_never_emits_a_key(monkeypatch):
    import python.harness.server as server

    async def _fake_load():
        return [{
            "id": "p1", "type": "remote", "name": "Gemini",
            "priority": 1, "status": "online", "locked": False,
            "config": {"apiKey": "AIzaTOTALLYSECRET", "model": "gemini-2.0-pro"},
        }]

    monkeypatch.setattr(server, "load_inference_providers", _fake_load)
    raw = await server.handle_inference_get_providers({}, "req-1")

    assert "AIzaTOTALLYSECRET" not in raw, "the key crossed the RPC boundary"
    provider = json.loads(raw)["result"]["providers"][0]
    assert provider["config"]["apiKey"] == REDACTED_SENTINEL
    assert provider["hasApiKey"] is True
    assert provider["config"]["model"] == "gemini-2.0-pro"


@pytest.mark.asyncio
async def test_detect_local_providers_never_emits_a_key(monkeypatch):
    """The sibling handler exists so the renderer can skip get_providers, which
    makes it the same leak behind a different name."""
    import python.harness.server as server

    async def _fake_load():
        return [{
            "id": "p1", "type": "local", "name": "Ollama",
            "priority": 1, "status": "online", "locked": False,
            "config": {"apiKey": "AIzaTOTALLYSECRET", "engine": "ollama"},
        }]

    async def _noop():
        return None

    monkeypatch.setattr(server, "load_inference_providers", _fake_load)
    monkeypatch.setattr(server, "_auto_detect_and_verify_providers", _noop)
    raw = await server.handle_inference_detect_local_providers({}, "req-2")

    assert "AIzaTOTALLYSECRET" not in raw
    assert json.loads(raw)["result"]["providers"][0]["config"]["apiKey"] == REDACTED_SENTINEL


def test_git_provider_row_resolution_is_still_plaintext():
    """Redaction happens at transport. The resolution itself has to stay
    assertable, or a broken credential lookup looks exactly like a working
    one."""
    from python.harness.server import _git_provider_from_row

    row = {
        "id": "g1", "name": "GitHub", "endpoint": "https://github.com",
        "username": "steve", "api_key": "inline-token", "priority": 1,
        "locked": False, "credential_id": "cred-1",
        "cred_config": {"api_key": "credential-token"},
    }
    assert _git_provider_from_row(row)["api_key"] == "credential-token"
    row_no_cred = dict(row, credential_id=None, cred_config=None)
    assert _git_provider_from_row(row_no_cred)["api_key"] == "inline-token"

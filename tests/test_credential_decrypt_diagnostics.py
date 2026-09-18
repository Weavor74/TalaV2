"""
A decryption failure has to say which value failed, and why.

The message used to be "credential_crypto: a value failed to decrypt
(InvalidTag). Leaving it as-is rather than dropping it." -- no field, no store,
no cause. It fired twice on every autonomous goal run for a whole session and
nobody could tell from it whether one credential was affected or fifty.

The cause turned out to be structural rather than corrupt data: the key lives in
the user-data root (``vault/.keys/credential_key.bin``) and the ciphertext lives
in Postgres, so pointing ``TALA_USER_DATA`` at a new directory mints a fresh key
while the database keeps values encrypted with the old one. Everything then
fails to decrypt, safely and silently, forever.

An error nobody can act on is one everybody learns to scroll past, which is the
same failure as a silent success with more noise.
"""
from __future__ import annotations

import logging


from python.harness.credential_crypto import (
    _decrypt_value,
    _encrypt_value,
    clear_decrypt_failure_cache,
    decrypt_config,
    is_encrypted,
)

_KEY_A = b"A" * 32
_KEY_B = b"B" * 32


def test_a_value_encrypted_with_another_key_names_the_field(caplog):
    clear_decrypt_failure_cache()
    envelope = _encrypt_value(_KEY_A, "sk-secret-value")

    with caplog.at_level(logging.ERROR):
        assert _decrypt_value(_KEY_B, envelope, "apiKey") is None

    message = caplog.text
    assert "apiKey" in message, "the failing field must be named"
    assert "InvalidTag" in message
    assert "does not match the one that encrypted it" in message


def test_it_explains_the_key_and_the_value_live_in_different_places(caplog):
    """
    The actionable part. Without it, InvalidTag reads like data corruption and
    sends someone hunting for a damaged row that is perfectly intact.
    """
    clear_decrypt_failure_cache()
    envelope = _encrypt_value(_KEY_A, "sk-secret-value")

    with caplog.at_level(logging.ERROR):
        _decrypt_value(_KEY_B, envelope, "password")

    assert "credential_key.bin" in caplog.text or "user-data root" in caplog.text
    assert "Postgres" in caplog.text or "database" in caplog.text or "HTML-NN" in caplog.text


def test_the_field_name_survives_the_config_walk(caplog, monkeypatch):
    """decrypt_config knows the field name; it has to pass it down."""
    import python.harness.credential_crypto as cc

    clear_decrypt_failure_cache()
    monkeypatch.setattr(cc, "load_or_create_key", lambda: _KEY_B)
    config = {"endpoint": "https://example.invalid", "apiKey": _encrypt_value(_KEY_A, "sk-x")}

    with caplog.at_level(logging.ERROR):
        out = decrypt_config(config)

    assert "apiKey" in caplog.text
    # Never destroy on failure: the unreadable value comes back untouched.
    assert is_encrypted(out["apiKey"])
    assert out["endpoint"] == "https://example.invalid"


def test_a_matching_key_decrypts_without_logging_an_error(caplog, monkeypatch):
    import python.harness.credential_crypto as cc

    clear_decrypt_failure_cache()
    monkeypatch.setattr(cc, "load_or_create_key", lambda: _KEY_A)
    config = {"apiKey": _encrypt_value(_KEY_A, "sk-real")}

    with caplog.at_level(logging.ERROR):
        out = decrypt_config(config)

    assert out["apiKey"] == "sk-real"
    assert "failed to decrypt" not in caplog.text


def test_a_non_invalidtag_failure_reports_its_own_cause(caplog):
    """A malformed envelope is a different problem and must not claim key mismatch."""
    clear_decrypt_failure_cache()
    with caplog.at_level(logging.ERROR):
        assert _decrypt_value(_KEY_A, {"n": "!!not-base64!!", "ct": "@@"}, "token") is None

    assert "token" in caplog.text
    assert "does not match the one that encrypted it" not in caplog.text


def test_a_bad_ciphertext_is_logged_only_once(caplog, monkeypatch):
    """
    Chat and settings re-decrypt the same sealed values on every request.
    Logging InvalidTag each time is noise that trains people to ignore a real
    key/data split. First failure stays at ERROR; the rest stay quiet.
    """
    clear_decrypt_failure_cache()
    envelope = _encrypt_value(_KEY_A, "sk-secret-value")

    with caplog.at_level(logging.ERROR):
        assert _decrypt_value(_KEY_B, envelope, "apiKey") is None
        assert _decrypt_value(_KEY_B, envelope, "apiKey") is None
        assert _decrypt_value(_KEY_B, envelope, "apiKey") is None

    assert caplog.text.count("failed to decrypt") == 1
    assert "will not be re-logged" in caplog.text


def test_distinct_bad_ciphertexts_each_get_one_log(caplog):
    clear_decrypt_failure_cache()
    one = _encrypt_value(_KEY_A, "alpha")
    two = _encrypt_value(_KEY_A, "beta")

    with caplog.at_level(logging.ERROR):
        assert _decrypt_value(_KEY_B, one, "apiKey") is None
        assert _decrypt_value(_KEY_B, two, "token") is None
        assert _decrypt_value(_KEY_B, one, "apiKey") is None

    assert caplog.text.count("failed to decrypt") == 2
    assert "apiKey" in caplog.text
    assert "token" in caplog.text

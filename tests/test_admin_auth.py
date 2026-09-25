"""
tests/test_admin_auth.py
=========================
Security regression coverage for python/harness/admin_auth.py — the shared
secret gating admin-level self-modification RPCs (system.trigger_update,
system.download_update) and, when enabled, the entire MCP tool-call surface
(python/harness/mcp_server.py).

Covers two real, confirmed vulnerabilities found in a security audit of that
surface:
  1. The secret file was created via Path.write_text(), which relies on the
     process umask instead of an explicit mode — confirmed on the real VPS
     deployment (root's umask there is 022), meaning a freshly-created
     secret file would land as world-readable (644), letting any other
     local user/process on the machine read it straight off disk.
  2. The comparison was a plain `==`, which short-circuits on the first
     mismatched byte — a timing side-channel (CWE-208) for a secret gating
     git-pull/git-archive and a ~238-tool call surface.

Uses TALA_HARNESS_ADMIN_SECRET_FILE to sandbox every test to a temp path —
never touches the real dev vault/.keys directory.
"""
from __future__ import annotations

import os
import sys

import pytest

from python.harness import admin_auth


@pytest.fixture(autouse=True)
def _isolated_secret_file(tmp_path, monkeypatch):
    """Point every test at a throwaway secret file/dir, and make sure no
    stray TALA_HARNESS_ADMIN_SECRET env var from the real environment leaks
    into a test that wants to exercise file-based creation."""
    secret_path = tmp_path / "vault" / ".keys" / "harness_admin_secret.txt"
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET_FILE", str(secret_path))
    monkeypatch.delenv("TALA_HARNESS_ADMIN_SECRET", raising=False)
    monkeypatch.delenv("TALA_USER_DATA", raising=False)
    return secret_path


def test_harness_admin_secret_path_honors_env_override(_isolated_secret_file):
    assert admin_auth._harness_admin_secret_path() == _isolated_secret_file.resolve()


def test_load_or_create_generates_and_persists_secret(_isolated_secret_file):
    first = admin_auth._load_or_create_harness_admin_secret()
    assert first  # non-empty
    assert _isolated_secret_file.exists()

    second = admin_auth._load_or_create_harness_admin_secret()
    assert second == first, "secret must persist across calls, not regenerate"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits don't apply on Windows")
def test_secret_file_created_owner_only(_isolated_secret_file):
    """
    Regression: the file used to be created via Path.write_text(), which
    lands at the umask-derived default mode (644 under the common 022
    umask found on the real VPS) — world-readable. Must be 600.
    """
    admin_auth._load_or_create_harness_admin_secret()
    mode = os.stat(_isolated_secret_file).st_mode & 0o777
    assert mode == 0o600, f"expected owner-only 0600, got {oct(mode)}"

    parent_mode = os.stat(_isolated_secret_file.parent).st_mode & 0o777
    assert parent_mode == 0o700, f"expected owner-only 0700 dir, got {oct(parent_mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits don't apply on Windows")
def test_loosely_permissioned_existing_file_gets_self_healed(_isolated_secret_file):
    """A secret file created before this hardening existed (loose umask-derived
    permissions) must get tightened up the next time it's touched, without
    requiring a manual chmod on every already-deployed machine."""
    _isolated_secret_file.parent.mkdir(parents=True, exist_ok=True)
    _isolated_secret_file.write_text("pre-existing-secret-value", encoding="utf-8")
    os.chmod(_isolated_secret_file, 0o644)
    assert os.stat(_isolated_secret_file).st_mode & 0o777 == 0o644

    secret = admin_auth._load_or_create_harness_admin_secret()
    assert secret == "pre-existing-secret-value", "must not silently regenerate an existing secret"
    assert os.stat(_isolated_secret_file).st_mode & 0o777 == 0o600


def test_env_var_secret_takes_precedence_and_skips_file(_isolated_secret_file, monkeypatch):
    monkeypatch.setenv("TALA_HARNESS_ADMIN_SECRET", "env-provided-secret")
    secret = admin_auth._load_or_create_harness_admin_secret()
    assert secret == "env-provided-secret"
    assert not _isolated_secret_file.exists(), "env var path must not touch the filesystem"


def test_require_harness_admin_secret_accepts_correct_value(_isolated_secret_file):
    real_secret = admin_auth._load_or_create_harness_admin_secret()
    assert admin_auth.require_harness_admin_secret({"harness_secret": real_secret}) is True


@pytest.mark.parametrize("bad_params", [
    {"harness_secret": "totally-wrong"},
    {"harness_secret": ""},
    {},
    {"harness_secret": None},
])
def test_require_harness_admin_secret_rejects_bad_values(_isolated_secret_file, bad_params):
    admin_auth._load_or_create_harness_admin_secret()
    assert admin_auth.require_harness_admin_secret(bad_params) is False


def test_require_harness_admin_secret_rejects_prefix_match(_isolated_secret_file):
    """A caller supplying a truncated/prefix guess of the real secret must
    still be rejected outright — sanity check that comparison is exact,
    not e.g. accidentally using startswith() semantics anywhere upstream."""
    real_secret = admin_auth._load_or_create_harness_admin_secret()
    assert admin_auth.require_harness_admin_secret({"harness_secret": real_secret[:-1]}) is False
    assert admin_auth.require_harness_admin_secret({"harness_secret": real_secret + "x"}) is False

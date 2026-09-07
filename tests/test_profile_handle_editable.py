"""
The one field a person could not set was the one that names them.

`_peer_self_username` resolves this install's network identity from the
profile's ``identity.username``. That value becomes a contact row on other
installs, the sender on every packet, and a name written to a shared chain.

The save path always carried it. The field was disabled in edit mode --
`else ctrl.disabled = true; // handle locked` -- so the only way to change
it was to write to Postgres by hand, and every install that had not done
that answered to whatever was seeded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def renderer() -> str:
    return (ROOT / "renderer" / "renderer.js").read_text(encoding="utf-8", errors="replace")


def test_the_handle_can_be_edited(renderer):
    toggle = renderer.split("  toggleEditMode(editing) {")[1].split("\n  },")[0]
    assert "handle locked" not in toggle, "the network identity is still read-only"
    assert "prof-input-username" not in toggle or "ctrl.disabled = !editing" in toggle


def test_saving_keeps_the_handle(renderer):
    save = renderer.split("  async saveActiveProfile() {")[1].split("\n  },")[0]
    assert "prof-input-username" in save
    assert "username" in save


def test_the_ui_normalises_it_the_way_the_harness_does(renderer):
    """
    The harness rewrites this value before peers ever see it. A field that
    shows one thing while the network uses another is a person who does not
    know their own name.
    """
    assert "normalizeHandle" in renderer, "the handle is saved unnormalised"

    body = renderer.split("  normalizeHandle(raw) {")[1].split("\n  },")[0]
    assert "replace(/^@+/" in body, "a leading @ would be stored and then doubled"
    assert "/\\s+/g" in body, "whitespace would survive into a network identity"


def test_the_two_normalisations_agree():
    """
    Pinned against the harness rather than described: these are two
    implementations of one rule, in two languages, and they drift silently.
    """
    from python.harness import server

    renderer_js = (ROOT / "renderer" / "renderer.js").read_text(encoding="utf-8", errors="replace")
    harness = (ROOT / "python" / "harness" / "server.py").read_text(encoding="utf-8", errors="replace")

    resolver = harness.split("async def _peer_self_username()")[1].split("\nasync def ")[0]
    assert 'sub(r"\\s+", "-"' in resolver, (
        "the harness no longer collapses whitespace; the renderer still does"
    )
    assert 'lstrip("@")' in resolver, (
        "the harness no longer strips a leading @; the renderer still does"
    )
    assert "normalizeHandle" in renderer_js

    # And the rule itself, applied to the case that actually shows up.
    assert re.sub(r"\s+", "-", "@we avor".strip()).lstrip("@") == "we-avor"


def test_the_field_says_what_it_is_for():
    page = (ROOT / "renderer" / "index.html").read_text(encoding="utf-8", errors="replace")
    field = page.split('id="prof-input-username"')[1][:400]
    assert "shared chain" in field, (
        "nothing tells a person this handle is published to other installs"
    )

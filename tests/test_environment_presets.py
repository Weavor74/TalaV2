"""
The environment buttons are presets that do something, and lockdown is separate.

UI-044: `USB (ISOLATED)` / `LOCAL (SYSTEM)` / `REMOTE (CLOUD)` were a CSS class
toggle and a log line. That mattered more than a dead control usually does,
because **USB (ISOLATED) reads as a security guarantee** -- someone selecting it
would reasonably believe they had switched the system into an isolated mode,
and nothing changed.

UI-109, Steven (Q-25): *"If you select the USB button you are running from a
USB and all data stored is on USB. The embedded engine is all we have access
to. Think of it as a troubleshooting mode... It's also okay to think of it as
presets."*

UI-112, on keeping lockdown separate: *"Lockdown is a security override. This
should probably include the strictest guardrail settings possible. Local is
just the operational environment."*

UI-045: `EXPORT`, `IMPORT` and `APPLY CHANGES` had no handlers anywhere.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from python.harness import environment as env

INDEX = Path("renderer/index.html")
RENDERER = Path("renderer/renderer.js")


# -- the presets --------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("usb", ["embedded"]),
    ("local", ["embedded", "local"]),
    ("remote", ["embedded", "local", "remote"]),
])
def test_each_preset_permits_what_steven_specified(name, expected):
    assert env.build_state(name, False)["permitted_inference"] == expected


def test_usb_is_narrower_than_local():
    """"The embedded engine is all we have access to" -- USB is embedded only,
    not merely local, because it doubles as a troubleshooting mode."""
    usb = set(env.build_state("usb", False)["permitted_inference"])
    local = set(env.build_state("local", False)["permitted_inference"])
    assert usb < local


def test_each_preset_sets_storage_too():
    """A preset sets data root, inference and storage together."""
    assert env.build_state("usb", False)["permitted_storage"] == "usb"
    assert env.build_state("local", False)["permitted_storage"] == "local"
    assert env.build_state("remote", False)["permitted_storage"] == "any"


def test_an_unknown_environment_falls_back_to_local():
    """LOCAL is the conservative middle: not the narrowest, not the widest."""
    assert env.normalise("nonsense") == env.LOCAL
    assert env.normalise(None) == env.LOCAL
    assert env.normalise("REMOTE") == env.REMOTE


# -- lockdown is a separate axis ---------------------------------------------

def test_lockdown_restricts_remote_but_is_not_the_local_preset():
    """They land on the same inference restriction by different routes, and
    that is not a reason to merge them: the reason you are restricted is part
    of what the state means."""
    locked = env.build_state("remote", True)
    local = env.build_state("local", False)

    assert locked["permitted_inference"] == local["permitted_inference"]
    assert locked["environment"] == "remote"
    assert locked["restricted_by"] == "lockdown"
    assert local["restricted_by"] == "environment"


def test_lockdown_cannot_widen_a_narrow_preset():
    """USB is already tighter than lockdown; engaging it must not loosen."""
    assert env.build_state("usb", True)["permitted_inference"] == ["embedded"]


def test_lockdown_does_not_move_the_data_root():
    """Moving a user's data is not a safety measure."""
    for name in env.ENVIRONMENTS:
        plain = env.build_state(name, False)
        locked = env.build_state(name, True)
        assert plain["permitted_storage"] == locked["permitted_storage"]


# -- enforcement --------------------------------------------------------------

def test_blocked_providers_are_returned_not_dropped():
    """A provider that vanishes from the chain with no explanation is
    indistinguishable from one that was never configured."""
    providers = [
        {"id": "a", "type": "remote", "name": "Gemini"},
        {"id": "b", "type": "local", "name": "Ollama"},
        {"id": "c", "type": "embedded", "name": "Embedded"},
    ]
    allowed, blocked = env.filter_providers(providers, env.build_state("local", False))

    assert [p["id"] for p in allowed] == ["b", "c"]
    assert [p["id"] for p in blocked] == ["a"]


def test_usb_permits_only_the_embedded_engine():
    providers = [
        {"id": "a", "type": "remote", "name": "Gemini"},
        {"id": "b", "type": "local", "name": "Ollama"},
        {"id": "c", "type": "embedded", "name": "Embedded"},
    ]
    allowed, blocked = env.filter_providers(providers, env.build_state("usb", False))
    assert [p["id"] for p in allowed] == ["c"]
    assert len(blocked) == 2


def test_the_restriction_is_applied_where_the_chain_is_built():
    """Filtering at the panel would leave the restriction cosmetic, which is
    what it was."""
    src = Path("python/harness/server.py").read_text(encoding="utf-8")
    block = src.split("request_provider_id = str(params.get", 1)[0][-2500:]
    assert "filter_providers" in block
    assert "_env.get_state()" in block


def test_a_restriction_that_blocks_everything_still_leaves_the_embedded_engine():
    """Never leave the user with nothing to talk to."""
    src = Path("python/harness/server.py").read_text(encoding="utf-8")
    block = src.split("request_provider_id = str(params.get", 1)[0][-2500:]
    assert "falling back to" in block


def test_the_setting_has_its_own_row_not_a_shared_blob():
    """The profile data appeared to vanish because one save overwrote a whole
    config object (UI-056); this is exactly the kind of value that would go the
    same way."""
    src = Path("python/harness/db.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app_settings" in src


# -- the panel ----------------------------------------------------------------

def test_the_buttons_call_the_backend():
    js = RENDERER.read_text(encoding="utf-8")
    assert "environment.set" in js
    assert "setEnvironment" in js
    block = js.split("const envBtns = document.querySelectorAll", 1)[1][:400]
    assert "this.setEnvironment(btn.dataset.env)" in block


def test_the_panel_reads_the_stored_state_rather_than_assuming():
    """index.html marks LOCAL active; the stored value may be anything."""
    js = RENDERER.read_text(encoding="utf-8")
    assert "loadEnvironment" in js
    assert "environment.get" in js


def test_a_failed_change_is_reverted_in_the_ui():
    """The UI must not show a preset that was never applied."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("async setEnvironment(env)", 1)[1][:800]
    assert "this.loadEnvironment()" in block


def test_there_is_a_lockdown_control(html_text=None):
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settings-lockdown-toggle"' in html
    assert "security override" in html


def test_apply_changes_is_gone():
    """It was the primary action of the whole settings screen, had no handler,
    and taught the wrong model: that edits were staged awaiting commit, when
    every panel already persists on change."""
    html = INDEX.read_text(encoding="utf-8")
    js = RENDERER.read_text(encoding="utf-8")
    rendered = re.sub(r"<!--.*?-->", "", html, flags=re.S)

    assert "btn-settings-apply" not in rendered
    assert "APPLY CHANGES" not in rendered
    assert "btn-settings-apply" not in js


def test_export_and_import_have_handlers():
    js = RENDERER.read_text(encoding="utf-8")
    assert "exportSettings" in js
    assert "importSettings" in js
    assert "#btn-settings-export" in js
    assert "#btn-settings-import" in js


def test_the_export_omits_credentials():
    """An export that decrypted them would be a plaintext copy of every secret
    in the app sitting in the user's downloads folder."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("async exportSettings()", 1)[1][:1800]
    assert "delete copy.api_key" in block
    assert "delete copy.config" in block
    assert "Credentials were not included" in block


def test_the_import_does_not_replace_provider_lists():
    """Replacing them would silently delete credentials the export never
    contained."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("async importSettings(event)", 1)[1][:1800]
    assert "environment.set" in block
    assert "inference.save_providers" not in block


def test_the_scope_buttons_admit_they_do_nothing():
    """Still cosmetic. Saying so beats logging a line that implies otherwise."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("const scopeBtns = document.querySelectorAll", 1)[1][:900].lower()
    assert "implemented" in block, "the scope buttons still imply they do something"
    assert "global" in block

"""
The Vault view exists, is wired, and does not claim what it cannot do.

UI-036: the entire view was eight lines of placeholder HTML -- no list
container, no drop zone, no file input, and no JS module at all. The only
`vault` references in renderer.js were unrelated.

These tests are the same shape as the UI map drift tests: every element the JS
reaches for must exist in the markup, because a `qs()` returning null fails
silently and leaves a panel that renders and does nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = Path("renderer/index.html")
RENDERER = Path("renderer/renderer.js")


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js():
    return RENDERER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def module_source(js):
    return js.split("const VaultManager = {", 1)[1].split("\nconst ", 1)[0]


def test_the_view_is_no_longer_a_placeholder(html):
    view = html.split('id="view-vault"', 1)[1].split("</section>", 1)[0]
    assert "Drop files here to store them securely" not in view
    assert 'id="vault-list"' in view
    assert 'id="vault-editor"' in view


def test_there_is_a_javascript_module(js):
    assert "const VaultManager = {" in js
    assert "initSafe('VaultManager'" in js


def test_the_view_loads_when_it_is_opened(js):
    """A module that is never told to load is the same as no module."""
    block = js.split("if (id === 'vault')", 1)[1][:120]
    assert "VaultManager.load()" in block


@pytest.mark.parametrize("element_id", [
    "vault-list", "vault-search-input", "vault-editor", "vault-empty",
    "vault-doc-title", "vault-doc-tags", "vault-doc-body",
    "vault-grant-remote", "vault-grant-contacts", "vault-status",
    "btn-vault-new", "btn-vault-save", "btn-vault-delete",
])
def test_every_element_the_module_reaches_for_exists(html, module_source, element_id):
    assert f'id="{element_id}"' in html, f"{element_id} is referenced but not in the markup"
    assert f"#{element_id}" in module_source, f"{element_id} exists but nothing uses it"


def test_no_dangling_selectors(html, module_source):
    """The inverse: a qs() for an id that does not exist returns null and the
    panel quietly does nothing."""
    missing = [
        sel for sel in set(re.findall(r"qs\('#([a-z0-9-]+)'\)", module_source))
        if f'id="{sel}"' not in html
    ]
    assert not missing, f"selectors with no matching element: {missing}"


# -- what the panel claims ---------------------------------------------------

def test_the_network_isolated_badge_is_gone(html):
    """It asserted a control that never worked (UI-039) and is no longer the
    rule -- sharing is governed at the exit, not by a global isolation flag."""
    view = html.split('id="view-vault"', 1)[1].split("</section>", 1)[0]
    # Comments stripped: the comment explaining the removal necessarily quotes
    # what was removed.
    rendered = re.sub(r"<!--.*?-->", "", view, flags=re.S)
    assert "Network Isolated" not in rendered


def test_the_badge_says_something_true(html):
    view = html.split('id="view-vault"', 1)[1].split("</section>", 1)[0]
    assert "Sealed" in view
    assert "never leaves" in view


def test_the_access_controls_are_present_and_default_off(html):
    """Default deny has to be visible, or the user cannot tell it is on."""
    view = html.split('id="view-vault"', 1)[1].split("</section>", 1)[0]
    block = view.split('id="vault-grant-remote"', 1)[1][:400]
    assert "checked" not in block, "the remote grant renders pre-ticked"
    assert "Off by default" in view


def test_the_contact_field_says_grants_do_not_cascade(html):
    view = html.split('id="view-vault"', 1)[1].split("</section>", 1)[0]
    assert "never reach the vault" in view


# -- honesty in the module ---------------------------------------------------

def test_grants_are_rendered_from_storage_not_from_the_form(module_source):
    """After a save the panel re-reads what was stored, so a grant that failed
    to persist cannot look applied."""
    assert "loadGrants" in module_source
    block = module_source.split("async loadGrants(", 1)[1][:500]
    assert "_documents.find" in block


def test_the_listing_carries_the_stored_grants():
    """Without this the access controls render as "off" whatever they are set
    to -- the most dangerous direction for that mistake."""
    src = Path("python/harness/vault_nodes.py").read_text(encoding="utf-8")
    block = src.split("def list_documents(", 1)[1].split("def read_document(", 1)[0]
    assert '"grants"' in block


def test_a_failed_open_is_not_shown_as_an_empty_document(module_source):
    block = module_source.split("async open(docId)", 1)[1][:900]
    assert "doc?.success" in block
    assert "setStatus" in block


def test_a_save_that_loses_its_grants_does_not_report_success(module_source):
    """The document write and the grant write are separate calls and separate
    failures."""
    block = module_source.split("async save()", 1)[1][:2200]
    assert "granted?.success" in block
    assert "access settings were not applied" in block


def test_delete_warns_that_there_is_no_backup(module_source):
    """The vault is never synced, so a delete is final in a way the rest of the
    app is not."""
    block = module_source.split("async remove()", 1)[1][:600]
    assert "never backed up" in block


def test_unsaved_changes_are_not_dropped_silently(module_source):
    assert "_dirty" in module_source
    assert "Discard unsaved changes" in module_source


def test_untrusted_text_is_escaped(module_source):
    """Titles and tags are the user's own words, but they still go into
    innerHTML."""
    block = module_source.split("renderList()", 1)[1].split("async open(", 1)[0]
    assert "escapeHtml(d.title" in block
    assert "escapeHtml(d.id)" in block
    raw = re.findall(r"\$\{d\.(\w+)\}", block)
    assert not raw, f"unescaped fields interpolated: {raw}"

"""
The Vault: HTML-NN labelled vault, sealed at rest, and four different edges.

Steven: *"Vault section does not work at all. It is meant for storage of
editable private documents. Things the local LLM could reference, but nothing
gets shared outside."* And, on what to build it on: *"We started that framework
with the html-nn being labeld as vault"*.

"Nothing gets shared outside" turned out to have four edges that do not get the
same answer, and Steven decided each one:

* remote models — refused by default, allowed per document by explicit grant
* local search — yes, indexed; the index holds plaintext and never leaves
* backup and sync — never, sealed or otherwise
* contacts — only by explicit per-document invite

The old design gated *reads* on a global network-isolation flag that was
permanently false (UI-102), so the vault was unreachable by construction; and
had the flag worked, the vault would have been unusable whenever the app was
online. Sharing is a property of the exit, not of reading.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SECRET = "Passport number X1234567, and the spare key is under the third flowerpot."


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import sdk.agent.documentation_nodes as dn
    from python.harness import vault_nodes as vn

    nodes = tmp_path / "nodes"
    nodes.mkdir()
    monkeypatch.setattr(dn, "resolve_active_nodes_dir", lambda: nodes)
    monkeypatch.setattr(vn, "_nodes_dir", lambda: nodes)
    return vn


@pytest.fixture
def doc(vault):
    vault.write_document("Personal Papers", SECRET, tags=["ids", "private"])
    return "vault-personal-papers"


# -- sealed at rest ----------------------------------------------------------

def test_the_body_is_not_readable_on_disk(vault, doc):
    """The property that matters when the USB stick is in someone else's hand."""
    path = vault._path_for(doc)
    raw = path.read_text(encoding="utf-8")

    assert SECRET not in raw
    assert "X1234567" not in raw
    assert "flowerpot" not in raw


def test_the_title_stays_readable(vault, doc):
    """A vault whose contents cannot be listed without the key is unusable."""
    raw = vault._path_for(doc).read_text(encoding="utf-8")
    assert "Personal Papers" in raw


def test_it_is_an_html_nn_labelled_vault(vault, doc):
    """Steven's framework: not a second kind of storage."""
    raw = vault._path_for(doc).read_text(encoding="utf-8")
    assert 'data-node-type="vault"' in raw
    assert 'data-vault="true"' in raw
    assert 'data-execution-authority="0.0"' in raw


def test_it_unseals_to_the_original(vault, doc):
    assert vault.read_document(doc)["body"] == SECRET


def test_writing_without_a_key_refuses_rather_than_writing_clear(vault, monkeypatch):
    import python.harness.credential_crypto as cc
    monkeypatch.setattr(cc, "load_or_create_key", lambda: None)

    with pytest.raises(RuntimeError, match="Refusing"):
        vault.write_document("Unsealed", "should never land on disk")


# -- listing and searching ---------------------------------------------------

def test_listing_never_carries_the_body(vault, doc):
    listing = vault.list_documents()
    assert len(listing) == 1
    assert listing[0]["title"] == "Personal Papers"
    assert SECRET not in json.dumps(listing)


def test_contents_are_searchable(vault, doc):
    hits = vault.search_documents("flowerpot")
    assert len(hits) == 1
    assert hits[0]["id"] == doc


def test_an_unparseable_document_is_listed_not_hidden(vault, doc):
    """A corrupt vault file must be visible as a problem, not silently absent."""
    vault._path_for(doc).write_text("<html>not a node</html>", encoding="utf-8")
    listing = vault.list_documents()
    assert len(listing) == 1
    assert listing[0].get("unreadable") or not listing[0].get("sealed")


# -- the id that caused a real bug -------------------------------------------

def test_an_already_prefixed_id_does_not_double_prefix(vault):
    """_safe_id prefixed unconditionally, so re-saving a document by its own id
    produced vault-vault-<name>: a second file. Grants written that way landed
    somewhere nothing read, and the original silently kept its old permissions
    -- the worst way for a permission edit to fail."""
    assert vault._safe_id("vault-personal-papers") == "vault-personal-papers"
    assert vault._safe_id("Personal Papers") == "vault-personal-papers"


def test_an_id_cannot_climb_out_of_the_nodes_directory(vault):
    for evil in ("../secrets", "a/b", "..\\..\\etc"):
        with pytest.raises(ValueError):
            vault._path_for(evil)


# -- grants: default deny ----------------------------------------------------

def test_a_new_document_grants_nothing(vault, doc):
    grants = vault.get_grants(doc)
    assert grants["remote_inference"] is False
    assert grants["contacts"] == []


def test_a_missing_document_grants_nothing(vault):
    assert vault.get_grants("vault-does-not-exist")["remote_inference"] is False


# -- the remote-model edge ---------------------------------------------------

def _provider(monkeypatch, kind, name="model"):
    from python.harness import db

    async def row(*a, **k):
        return {"type": kind, "name": name}
    monkeypatch.setattr(db, "fetchrow", row)


@pytest.mark.asyncio
async def test_a_local_model_reads_freely(vault, doc, monkeypatch):
    """The entire point of "things the local LLM could reference"."""
    _provider(monkeypatch, "local", "qwen3")
    await vault.assert_may_read(doc)


@pytest.mark.asyncio
async def test_a_remote_model_is_refused_without_a_grant(vault, doc, monkeypatch):
    _provider(monkeypatch, "remote", "Gemini")
    with pytest.raises(vault.VaultEgressError, match="Gemini"):
        await vault.assert_may_read(doc)


@pytest.mark.asyncio
async def test_an_explicit_grant_opens_it(vault, doc, monkeypatch):
    vault.set_grant(doc, remote_inference=True)
    _provider(monkeypatch, "remote", "Gemini")
    await vault.assert_may_read(doc)


@pytest.mark.asyncio
async def test_a_revoked_grant_closes_it_again(vault, doc, monkeypatch):
    vault.set_grant(doc, remote_inference=True)
    vault.set_grant(doc, remote_inference=False)
    _provider(monkeypatch, "remote", "Gemini")
    with pytest.raises(vault.VaultEgressError):
        await vault.assert_may_read(doc)


@pytest.mark.asyncio
async def test_an_unknown_provider_fails_closed(vault, doc, monkeypatch):
    """Not knowing where a prompt is going is not permission to put a private
    document into it."""
    from python.harness import db

    async def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(db, "fetchrow", boom)

    with pytest.raises(vault.VaultEgressError):
        await vault.assert_may_read(doc)


def test_a_grant_does_not_disturb_the_body(vault, doc):
    """The grant path rewrites the document, so it must go through the same
    sealing code rather than being a second, subtly different writer."""
    vault.set_grant(doc, remote_inference=True)

    assert vault.read_document(doc)["body"] == SECRET
    assert SECRET not in vault._path_for(doc).read_text(encoding="utf-8")


# -- the contact edge --------------------------------------------------------

def test_a_contact_needs_a_per_document_invite(vault, doc):
    assert not vault.may_share_with_contact(doc, "alice")
    vault.set_grant(doc, contacts=["alice"])
    assert vault.may_share_with_contact(doc, "alice")
    assert not vault.may_share_with_contact(doc, "bob")


def test_contact_names_normalise(vault, doc):
    vault.set_grant(doc, contacts=["Alice"])
    assert vault.may_share_with_contact(doc, "@alice")


# -- the sync edge -----------------------------------------------------------

def test_the_vault_and_its_index_never_leave():
    """Steven: "No -- vault never leaves the install." The Chroma directory is
    covered too: it holds unsealed vault bodies, so syncing it would export the
    vault by the back door."""
    from python.harness.server import is_local_only_artifact

    assert is_local_only_artifact("vault-personal-papers.html")
    assert is_local_only_artifact("nodes/vault-anything.html")
    assert is_local_only_artifact("data/chroma/index")
    assert not is_local_only_artifact("tool-web-search.html")


# -- the local-index edge ----------------------------------------------------

def test_vault_bodies_are_indexed_locally():
    """Steven chose this: the index holds plaintext and never leaves the
    machine, which is what makes the vault findable rather than merely
    storable."""
    src = Path("python/harness/retrieval.py").read_text(encoding="utf-8")
    assert "_is_vault_file" in src
    assert "_vault_node_for_index" in src


def test_an_unopenable_vault_document_does_not_break_indexing():
    """A vault the key no longer matches should degrade to unsearchable, not
    take the whole indexing pass down."""
    from python.harness.retrieval import _vault_node_for_index

    assert _vault_node_for_index(Path("nodes/vault-does-not-exist.html")) is None


# -- the agent's side --------------------------------------------------------

@pytest.mark.asyncio
async def test_the_agent_can_list_without_a_local_model(vault, doc, monkeypatch):
    """A vault whose titles cannot be seen is one nobody can ask about."""
    import sdk.agent.vault_tools as vt

    _provider(monkeypatch, "remote", "Gemini")
    result = await vt.vault_list()
    assert result["success"] is True
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_a_refusal_is_not_an_empty_document(vault, doc, monkeypatch):
    import sdk.agent.vault_tools as vt

    _provider(monkeypatch, "remote", "Gemini")
    result = await vt.vault_read(doc)

    assert result["success"] is False
    assert result.get("refused") is True
    assert "Gemini" in result["error"]
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_search_withholds_per_document(vault, doc, monkeypatch):
    """One withheld document must not hide the rest."""
    import sdk.agent.vault_tools as vt

    vault.write_document("Open Notes", "the flowerpot is fine to share")
    vault.set_grant("vault-open-notes", remote_inference=True)
    _provider(monkeypatch, "remote", "Gemini")

    result = await vt.vault_search("flowerpot")
    assert result["withheld"] == 1
    assert [h["id"] for h in result["hits"]] == ["vault-open-notes"]
    assert SECRET not in json.dumps(result)


def test_the_agent_cannot_write_or_delete():
    """Putting things in and taking them out stays with the user. A private
    document store an agent can silently rewrite is not one."""
    import ast

    tree = ast.parse(Path("sdk/agent/vault_tools.py").read_text(encoding="utf-8"))
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    for forbidden in ("write_document", "delete_document", "set_grant"):
        assert forbidden not in referenced, f"a vault tool calls {forbidden}"


# -- images (Steven: "I at least need to be able to put a image in") ---------

import base64
import hashlib
import struct
import zlib

#: The PNG file signature, written as byte values rather than escapes. An
#: earlier version used escape sequences and they were collapsed into real
#: control characters while the file was being edited, which broke the module.
PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])
NUL = bytes([0])


def _png(width=32, height=24):
    """A real PNG, built here so the test needs no fixture file."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b""
    for y in range(height):
        # A leading zero is the PNG "no filter" byte for each scanline.
        raw += NUL + bytes(
            v for x in range(width) for v in (x * 8 % 256, y * 9 % 256, 64))

    return (PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_an_image_round_trips_byte_for_byte(vault):
    png = _png()
    vault.write_document(
        "Passport Scan", base64.b64encode(png).decode("ascii"),
        content_type="image/png", filename="passport.png",
    )

    doc = vault.read_document("vault-passport-scan")
    assert doc["content_type"] == "image/png"
    assert doc["filename"] == "passport.png"
    assert hashlib.sha256(base64.b64decode(doc["body"])).digest() \
        == hashlib.sha256(png).digest()


def test_an_image_is_sealed_like_anything_else(vault):
    """Base64 is a string, so the same sealing works -- one storage path, and
    one thing to get right about encryption rather than two."""
    encoded = base64.b64encode(_png()).decode("ascii")
    vault.write_document("Scan", encoded, content_type="image/png", filename="s.png")

    raw = vault._path_for("vault-scan").read_text(encoding="utf-8")
    assert encoded[:64] not in raw
    assert "iVBORw0KGgo" not in raw, "the PNG signature is readable on disk"


def test_a_permission_change_does_not_corrupt_an_image(vault):
    """set_grant rewrites the whole document; dropping the content type would
    silently turn an image into a text document."""
    png = _png()
    vault.write_document("Scan", base64.b64encode(png).decode("ascii"),
                         content_type="image/png", filename="s.png")

    vault.set_grant("vault-scan", remote_inference=True)
    doc = vault.read_document("vault-scan")

    assert doc["content_type"] == "image/png"
    assert doc["filename"] == "s.png"
    assert base64.b64decode(doc["body"]) == png


def test_the_listing_says_what_kind_of_document_it_is(vault):
    """So the panel can pick a renderer without opening every document."""
    vault.write_document("Notes", "plain words")
    vault.write_document("Scan", base64.b64encode(_png()).decode("ascii"),
                         content_type="image/png", filename="s.png")

    by_id = {d["id"]: d for d in vault.list_documents()}
    assert by_id["vault-notes"]["content_type"] == "text/plain"
    assert by_id["vault-scan"]["content_type"] == "image/png"
    assert by_id["vault-scan"]["filename"] == "s.png"


def test_attachments_are_searched_by_name(vault):
    """Searching base64 finds nothing a person meant."""
    vault.write_document("Passport Scan", base64.b64encode(_png()).decode("ascii"),
                         content_type="image/png", filename="passport.png")

    hits = vault.search_documents("passport")
    assert len(hits) == 1
    assert "attachment" in hits[0]["snippet"]


def test_invalid_base64_is_refused(vault):
    with pytest.raises(ValueError, match="base64"):
        vault.write_document("Bad", "not base64!!!", content_type="image/png")


def test_an_unsupported_content_type_is_refused(vault):
    with pytest.raises(ValueError, match="Unsupported"):
        vault.write_document("Exe", base64.b64encode(b"MZ").decode("ascii"),
                             content_type="application/x-msdownload")


def test_an_oversized_attachment_is_refused(vault):
    """The whole document is sealed as one value and read in full every time it
    is opened, so a large image makes every listing slow."""
    payload = NUL * (vault.VAULT_MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError, match="MB"):
        vault.write_document("Huge", base64.b64encode(payload).decode("ascii"),
                             content_type="image/png")


def test_the_cap_measures_decoded_bytes_not_base64(vault):
    """The user cares about the size of their photograph, not its encoding
    overhead -- base64 inflates by a third."""
    import inspect
    src = inspect.getsource(vault.write_document)
    assert "b64decode" in src
    assert src.index("b64decode") < src.index("VAULT_MAX_ATTACHMENT_BYTES")

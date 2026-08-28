"""
Storage providers become an option that actually exists.

Steven: *"A user may never install or consume any of the storage/git/database
providers but they should have the option too."*

That reframes UI-068. The problem was never that nine provider types were
offered; it was that **none of them could work** and nothing said so -- the
panel accepted a bucket name and a secret key, stored them, and no code
anywhere could talk to a bucket. And UI-069: `storage_providers` appeared in
six places, every one of them storage or display, so nothing consumed it.

The answer is neither "ship nine cloud SDKs on a USB stick" nor "offer fewer".
Three types work with no new dependency; the rest are declared as optional
extras and say exactly what to install.
"""
from __future__ import annotations

import pytest

from python.harness import storage_clients as sc


# -- the capability table -----------------------------------------------------

def test_the_dependency_free_types_are_available():
    """A portable install must be able to back itself up to a second drive
    without installing anything."""
    available = {c["kind"] for c in sc.capabilities() if c["available"]}
    assert {"folder", "ftp", "webdav"} <= available


def test_nothing_heavy_is_required_by_default():
    for kind in ("folder", "ftp", "webdav"):
        assert sc.CAPABILITIES[kind].module is None


def test_an_unavailable_type_says_what_to_install():
    """"Not implemented" on its own tells the reader nothing they can act on."""
    for entry in sc.capabilities():
        if entry["available"]:
            continue
        assert entry["dependency"], f"{entry['kind']} gives no dependency name"
        assert entry["install_extra"], f"{entry['kind']} gives no install extra"
        assert entry["dependency"] in entry["reason"]
        assert entry["install_extra"] in entry["reason"]


def test_the_extras_are_declared_in_pyproject():
    from pathlib import Path

    toml = Path("pyproject.toml").read_text(encoding="utf-8")
    for entry in sc.capabilities():
        extra = entry["install_extra"]
        if extra:
            assert f"{extra} = [" in toml, f"the {extra!r} extra is not declared"


@pytest.mark.parametrize("raw,expected", [
    ("S3-Compatible", "s3"),
    ("MinIO", "s3"),
    ("SFTP / SSH", "sftp"),
    ("FTP/FTPS", "ftp"),
    ("Google Drive", "gdrive"),
    ("NFS", "folder"),
    ("Local", "folder"),
])
def test_panel_labels_map_onto_capabilities(raw, expected):
    """The panel offers prose labels; the registry is keyed by kind."""
    assert sc.normalise_kind(raw) == expected


# -- refusals are refusals, not stubs ----------------------------------------

def test_an_unimplemented_backend_refuses_and_names_the_package():
    with pytest.raises(sc.StorageError) as exc:
        sc.get_client({"provider_type": "s3", "credentials": {}})
    assert "boto3" in str(exc.value)


def test_an_unknown_type_lists_the_known_ones():
    with pytest.raises(sc.StorageError) as exc:
        sc.get_client({"provider_type": "carrier-pigeon", "credentials": {}})
    assert "folder" in str(exc.value)


def test_a_misconfigured_provider_says_which_field_is_missing():
    for kind, field in (("folder", "path"), ("ftp", "host"), ("webdav", "endpoint")):
        with pytest.raises(sc.StorageError) as exc:
            sc.get_client({"provider_type": kind, "credentials": {}})
        assert field in str(exc.value)


# -- the folder client, which is the one a portable install needs -------------

def test_a_folder_probe_reports_writability(tmp_path):
    client = sc.get_client({
        "provider_type": "folder", "credentials": {"path": str(tmp_path / "dest")},
    })
    assert "Writable" in client.probe()
    assert (tmp_path / "dest").exists()


def test_a_folder_probe_fails_on_an_unwritable_path(tmp_path, monkeypatch):
    client = sc.get_client({
        "provider_type": "folder", "credentials": {"path": str(tmp_path / "dest")},
    })

    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr("pathlib.Path.mkdir", boom)

    with pytest.raises(sc.StorageError, match="not writable"):
        client.probe()


def test_put_creates_parents_and_writes_bytes(tmp_path):
    client = sc.get_client({
        "provider_type": "folder", "credentials": {"path": str(tmp_path / "dest")},
    })
    client.put("a/b/c.html", b"<html>hi</html>")

    written = tmp_path / "dest" / "a" / "b" / "c.html"
    assert written.read_bytes() == b"<html>hi</html>"


def test_put_leaves_no_partial_file(tmp_path):
    """Write-then-rename, so an interrupted backup cannot leave a truncated
    file that looks complete."""
    client = sc.get_client({
        "provider_type": "folder", "credentials": {"path": str(tmp_path / "dest")},
    })
    client.put("node.html", b"data")
    assert not list((tmp_path / "dest").rglob("*.part"))


# -- the vault never leaves ---------------------------------------------------

def test_the_backup_withholds_vault_documents():
    from python.harness.server import is_local_only_artifact

    assert is_local_only_artifact("vault-passport.html")
    assert not is_local_only_artifact("tool-web-search.html")


def test_the_backup_path_uses_that_guard():
    """A backup that quietly included the vault would defeat the whole rule."""
    import inspect
    from python.harness import server

    src = inspect.getsource(server.handle_storage_backup_nodes)
    assert "is_local_only_artifact" in src
    assert "withheld" in src


def test_the_backup_offers_a_dry_run():
    """Sending every node somewhere is worth being able to preview."""
    import inspect
    from python.harness import server

    src = inspect.getsource(server.handle_storage_backup_nodes)
    assert "dry_run" in src


def test_a_partly_failed_backup_is_not_reported_as_success():
    import inspect
    from python.harness import server

    src = inspect.getsource(server.handle_storage_backup_nodes)
    assert '"success": not failed' in src

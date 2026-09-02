"""
A published bundle must not carry credentials (SEC-W0-04).

``blockchain/public/*.zip`` is uploaded to a public URL by
scripts/upload_vps.ps1 and advertised to every installation in version.json.
Three of them shipped ``docs/reference/root_.env.md`` -- a generated Markdown
page that prints the contents of ``.env`` verbatim -- and the root
``tala-windows.zip`` shipped ``.env`` itself.

The exclude list was not the problem: ``.env`` had been excluded since
24aff28. The bundles were stale artifacts built before that commit, and nothing
ever re-checked them. Hence this test, which looks at the artifact rather than
at the intent to produce a clean one.
"""

import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(REPO / "scripts"))
from build_bundles import SECRET_ENTRY  # noqa: E402


def _bundles():
    found = sorted((REPO / "blockchain" / "public").glob("*.zip"))
    root_bundle = REPO / "tala-windows.zip"
    if root_bundle.exists():
        found.append(root_bundle)
    return found


@pytest.mark.parametrize("bundle", _bundles(), ids=lambda p: p.name)
def test_bundle_carries_no_credentials(bundle):
    with zipfile.ZipFile(bundle) as zf:
        offenders = [n for n in zf.namelist() if SECRET_ENTRY.search(n)]
    assert not offenders, (
        f"{bundle.name} is served publicly and contains {offenders}. "
        f"Rebuild it with scripts/build_bundles.py."
    )


@pytest.mark.parametrize("bundle", _bundles(), ids=lambda p: p.name)
def test_bundle_is_still_a_readable_archive(bundle):
    """Stripping entries must not corrupt the artifact the updater downloads."""
    with zipfile.ZipFile(bundle) as zf:
        assert zf.testzip() is None, "archive is corrupt"
        names = zf.namelist()
    assert len(names) > 50, f"{bundle.name} has only {len(names)} entries"
    # The template is documentation, not a secret, and INSTALL.md points at it.
    assert any(n.endswith("package.json") for n in names)


def test_the_guard_would_reject_a_dirty_bundle(tmp_path):
    """The check that would have caught it -- proven to actually catch it."""
    from build_bundles import _assert_no_secrets

    dirty = tmp_path / "dirty.zip"
    with zipfile.ZipFile(dirty, "w") as zf:
        zf.writestr("README.md", "fine")
        zf.writestr(".env", "TALA_DB_URL=postgresql://tala:tala@localhost/tala")

    with pytest.raises(SystemExit, match="REFUSING TO PUBLISH"):
        _assert_no_secrets(str(dirty))


def test_the_guard_passes_a_clean_bundle(tmp_path):
    from build_bundles import _assert_no_secrets

    clean = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean, "w") as zf:
        zf.writestr("README.md", "fine")
        zf.writestr(".env.example", "# annotated template, no secrets")

    _assert_no_secrets(str(clean))  # must not raise

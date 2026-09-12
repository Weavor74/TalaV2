"""
bump_version.py — bump the version everywhere it lives, in one motion.

Sources of truth, kept in lockstep:
  * package.json / pyproject.toml          — the version number
  * CHANGELOG.md                           — the release notes (Unreleased → vX.Y.Z)
  * blockchain/public/version.json         — what every installed client polls:
    version, release_notes (from the changelog), download URLs, and the sha256
    of each platform zip.

The sha256 map is what makes app.update's integrity check real: main.js
fetches version.json independently and refuses a zip whose hash does not
match. Before this script published the map, the check was silently skipped
on every update. Run scripts/build_bundles.py first so the zips being hashed
are the zips being shipped; missing zips are a hard error unless
--allow-missing-bundles is passed (hashes for the absent platforms are then
omitted, and the updater falls back to warning for those platforms only).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

PLATFORM_ZIPS = {
    "windows": "tala-windows.zip",
    "macos": "tala-macos.zip",
    "linux": "tala-linux.zip",
}


def _extract_unreleased_notes(changelog_text: str) -> str:
    """The body of the ## [Unreleased] section, stripped."""
    match = re.search(
        r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)",
        changelog_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _promote_unreleased(changelog_text: str, new_version: str, date: str) -> str:
    """Rename Unreleased to the new version and open a fresh empty Unreleased."""
    return changelog_text.replace(
        "## [Unreleased]",
        f"## [Unreleased]\n\n## [{new_version}] - {date}",
        1,
    )


def compute_sha256_map(public_dir: str) -> tuple[dict, list]:
    """Hash each platform zip present in *public_dir*. Returns (map, missing)."""
    sha256_map, missing = {}, []
    for platform_key, zip_name in PLATFORM_ZIPS.items():
        zip_path = os.path.join(public_dir, zip_name)
        if not os.path.exists(zip_path):
            missing.append(zip_name)
            continue
        digest = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        sha256_map[platform_key] = digest.hexdigest()
    return sha256_map, missing


def update_version_json_hashes(public_dir: str) -> dict:
    """Re-stamp version.json's sha256 map from the zips now on disk.

    build_bundles.py calls this AFTER building, because the bump necessarily
    runs first (the zips must ship the new version in package.json), at which
    point the zips it could hash were the previous release's.
    """
    version_json_path = os.path.join(public_dir, "version.json")
    with open(version_json_path, "r", encoding="utf-8") as f:
        version_data = json.load(f)
    sha256_map, missing = compute_sha256_map(public_dir)
    if missing:
        sys.exit(f"Cannot stamp hashes: missing bundles {', '.join(missing)}.")
    version_data["sha256"] = sha256_map
    with open(version_json_path, "w", encoding="utf-8") as f:
        json.dump(version_data, f, indent=2)
        f.write("\n")
    return sha256_map


def bump_version(allow_missing_bundles: bool = False) -> None:
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_json_path = os.path.join(root_dir, "package.json")
    pyproject_toml_path = os.path.join(root_dir, "pyproject.toml")
    changelog_path = os.path.join(root_dir, "CHANGELOG.md")
    public_dir = os.path.join(root_dir, "blockchain", "public")
    version_json_path = os.path.join(public_dir, "version.json")

    # Read current version from package.json
    with open(package_json_path, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    current_version = pkg_data.get("version", "0.1.0")
    parts = current_version.split(".")
    if len(parts) != 3:
        parts = ["0", "1", "0"]

    # Bump patch version
    parts[2] = str(int(parts[2]) + 1)
    new_version = ".".join(parts)
    print(f"Bumping version from {current_version} to {new_version}")

    # Release notes come from the changelog, not a hardcoded string. An empty
    # Unreleased section is a stop condition: a release nobody wrote a line
    # about is a release nobody can audit later.
    if not os.path.exists(changelog_path):
        sys.exit("CHANGELOG.md not found. Write the release notes first.")
    with open(changelog_path, "r", encoding="utf-8") as f:
        changelog_text = f.read()
    release_notes = _extract_unreleased_notes(changelog_text)
    if not release_notes:
        sys.exit(
            "CHANGELOG.md has an empty [Unreleased] section. "
            "Write the release notes first — version.json publishes them."
        )

    # Hash the bundles being shipped. When called from build_bundles.py the
    # zips on disk are still the previous release's — it re-stamps the map via
    # update_version_json_hashes() after building.
    sha256_map, missing = compute_sha256_map(public_dir)
    if missing and not allow_missing_bundles:
        sys.exit(
            f"Missing release bundles: {', '.join(missing)}. "
            "Run scripts/build_bundles.py first, or pass --allow-missing-bundles "
            "to publish without hashes for those platforms (their updates will "
            "not be integrity-checked)."
        )

    # Update package.json
    pkg_data["version"] = new_version
    with open(package_json_path, "w", encoding="utf-8") as f:
        json.dump(pkg_data, f, indent=2)
        f.write("\n")

    # Update pyproject.toml (only the [project] version line)
    if os.path.exists(pyproject_toml_path):
        with open(pyproject_toml_path, "r", encoding="utf-8") as f:
            toml_content = f.read()
        toml_content = re.sub(
            r'^version\s*=\s*"[^"]+"',
            f'version = "{new_version}"',
            toml_content,
            count=1,
            flags=re.MULTILINE,
        )
        with open(pyproject_toml_path, "w", encoding="utf-8") as f:
            f.write(toml_content)

    # Promote Unreleased → the new version in the changelog.
    today = datetime.date.today().isoformat()
    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(_promote_unreleased(changelog_text, new_version, today))

    # Write version.json
    os.makedirs(public_dir, exist_ok=True)
    version_data = {
        "version": new_version,
        "release_notes": release_notes,
        "downloads": {
            "windows": "https://talacore.app/tala-windows.zip",
            "macos": "https://talacore.app/tala-macos.zip",
            "linux": "https://talacore.app/tala-linux.zip",
        },
        "sha256": sha256_map,
    }
    with open(version_json_path, "w", encoding="utf-8") as f:
        json.dump(version_data, f, indent=2)
        f.write("\n")

    print(f"Version bumped to {new_version}; version.json carries the changelog "
          f"notes and sha256 for {sorted(sha256_map)}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-missing-bundles", action="store_true")
    args = parser.parse_args()
    bump_version(allow_missing_bundles=args.allow_missing_bundles)

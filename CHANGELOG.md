# Changelog

All notable changes to Tala Cognitive Engine Room are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`scripts/bump_version.py` promotes the **Unreleased** section into a versioned
section on each release and publishes it as the `release_notes` field of
`blockchain/public/version.json`, so notes are written once, here, and the
download site inherits them.

## [Unreleased]

### Security
- `shell.run` now enforces its risk classification instead of only logging it:
  known-destructive command patterns are refused outright, and critical-tier
  commands require an explicit `approved=true`.
- The unsigned update path is gone. `system.download_update` (harness
  repackaging its live checkout) and the `system:apply_update` IPC channel were
  removed; every update — banner click or VPS push — now flows through the
  signed `app.update` path, pinned to the release host and verified against
  the sha256 map in version.json. `system.trigger_update` remains as a
  wake-up broadcast only.
- Deleted `set_gemini_key.py`, which wrote a provider API key to the database
  in plaintext, bypassing `credential_crypto`.

### Changed
- `scripts/bump_version.py` now publishes a `sha256` map for the release zips
  in version.json (the updater's integrity check was previously always
  skipped, because nothing ever published the hashes) and sources
  `release_notes` from this file.
- `pyproject.toml` is the single source of dependency truth: the drifted
  `python/requirements.txt`, `python/requirements_astrology.txt` and
  `sdk/requirements.txt` were deleted. GPU torch install guidance moved to
  INSTALL.md §9.

### Removed
- Repo-root scratch and patch debris (one-off `test_*`/`patch*`/`update_server*`
  scripts, scratch dirs, stale zips) and their auto-generated pages under
  `docs/reference/`. The `patch*/update_server*` scripts rewrote
  `python/harness/server.py` and `renderer/renderer.js` in place by string
  replacement and would corrupt current source if rerun.

## [0.1.83] - 2026-09-12

### Security
- All 7 P0 items from the 2026-09-09 production-readiness review: E2EE envelope
  method allowlist and unapproved-peer surface enforced per connection; default
  VPS endpoint moved to `wss://talacore.app/ws` with plaintext `ws://`
  off-machine refused unless explicitly allowed; `/chain/tx` requires the local
  admin secret or an ed25519 signature; update apply no longer trusts
  renderer-supplied bytes; web UI token mandatory on every interface.

### Added
- Observe-then-act desktop control, and saved workflows runnable from chat.
- Workflow editor generated from what the executors actually run.
- Self-maintaining documentation: documents detect when code moves out from
  under them, classified by purpose, with accountability measures.

### Fixed
- `desktop_action` reclassified out of the low-risk tool tier.
- Cross-platform P1s from the production-readiness review.

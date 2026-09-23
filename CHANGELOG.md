# Changelog

All notable changes to Tala Cognitive Engine Room are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`scripts/bump_version.py` promotes the **Unreleased** section into a versioned
section on each release and publishes it as the `release_notes` field of
`blockchain/public/version.json`, so notes are written once, here, and the
download site inherits them.

## [Unreleased]

## [0.1.84] - 2026-09-23

### Security
- The UI-audit campaign (PRs #8–#15): the renderer's IPC surface is closed —
  an unlisted channel is refused at preload and dotted RPC names must flow
  through `helpers.invoke`; distinct HTTP-UI and WebSocket tokens;
  `refuseSecretPath` walks inodes so not-yet-created secret paths are covered;
  `AuthorizationGate` is enforced at `run_tool` with fail-closed lease and
  network defaults; Swarm's destructive controls are WS-gated like Mission
  Control; credential `InvalidTag` decrypt failures no longer spam the log.
- The updater refuses downgrades: a published version that is not strictly
  newer than the installed one is rejected in the main process, independent of
  the renderer's banner comparison, so the pinned host cannot roll an install
  back to an older, vulnerable release.
- Release bundles no longer carry this install's live state: the twelve
  `nodes/settings-*.html` (the user's settings — previously silently reset to
  the build machine's on every OTA update), `emotion_state.json`,
  machine-synthesized `skills/skill_*.yaml`, and uuid-named workflows. Both
  packagers pin the exclusions.
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

### Fixed
- The Windows updater writes `%TEMP%\tala-update.log`; a failed update
  previously closed the app and left no trace of why.
- Honest MCP Bridge status, health next-action guidance, single-sourced
  WebSocket status, and the GPU chat gate (PRs #10–#11).
- MCP server arguments carrying Windows paths no longer lose their
  backslashes to posix-mode `shlex` splitting.

### Changed
- `server.py` cut-down slice 4: the eight inference-provider RPC handlers
  moved to `python/harness/provider_rpc.py`.
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

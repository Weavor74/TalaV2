# Changelog

All notable changes to Tala Cognitive Engine Room are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`scripts/bump_version.py` promotes the **Unreleased** section into a versioned
section on each release and publishes it as the `release_notes` field of
`blockchain/public/version.json`, so notes are written once, here, and the
download site inherits them.

## [Unreleased]

### Fixed
- Chain writes now land. `chains.record`/`flush` sent no `hash` or `creator`
  (and often an empty `counterparty`), so the node answered every one with a
  400 while the DB index row still said `written`; they now send a
  node-valid transaction (content hash, creator, `blockchain-ledger` as the
  counterparty fallback, the time it happened in the metadata). A transaction
  with no id or type comes back as an error instead of raising.
- Everything that talked to the local chain node hardcoded `127.0.0.1:8332`
  (anchoring, ghost persistence, the settings and persona anchors, the status
  dot, the contracts router, Electron). They now follow
  `TALA_CHAIN_BIND_HOST`/`TALA_CHAIN_BIND_PORT` like the node does, through
  one helper (`sdk/agent/ghost_persistence.local_chain_base_url`) and
  `electron/chain_endpoint.js`. Still tied to 8332: the renderer's chain viewer
  and the CSP in `index.html`/`main.js`, which would need to move together.
- Electron's Neural Node anchors (`file:write`/`file:delete`) sent no admin
  secret, so the node refused each with a 401 that was swallowed. They now
  send it, and a refusal is logged.
- CI lint was red on main: the previous chain fix grew `blockchain/src/app.js`
  past its line cap (1845 > 1822). The node is now split into modules and the
  cap lowered to 1400 (see Changed).
- `GET /chain/tx/:id` could not read ids containing `:` or `.` (for example
  `peer-directory:<wallet>`); they matched no route and returned 404.
- The chain node no longer wipes its ledger on restart once a signed
  transaction is on it: `loadChain` dropped each transaction's `signature` and
  `public_key`, which are part of the hashed block JSON, so the reloaded chain
  failed validation and was replaced with genesis only. A chain that is
  rejected on load is now copied to `chain.json.invalid-<time>` before the
  reset instead of being overwritten.
- Tala's mood no longer freezes. The emotional carry-over re-saved the carried
  label with a fresh timestamp, renewing its own 4.2 h window on every turn, so
  at any cadence under that the label never changed again (386 of 480 hourly
  turns were stale in simulation). A carry now keeps the time of the reading
  it came from, and the payload reports `engine_emotion` alongside it.
- A missing pyswisseph is now loud on the live mood path: an ERROR (repeating
  every 10 minutes, and at import) and `degraded: true` in the emotional state,
  instead of silently serving moods from an approximation that is wrong by up
  to ~170 degrees for Mercury and Venus.

### Changed
- The chain node is split into modules under `blockchain/src/` (`fs_util`,
  `request_limits`, `identity`, `chain_model`, `authority_set`,
  `peer_directory`); `app.js` went from 1845 to 1400 lines. `AuthoritySet` and
  `PeerDirectory` now take their data file and the ledger as arguments instead
  of reading globals. **`scripts/upload_vps.ps1` now ships every file in
  `blockchain/src/`** (syntax-checking each first) -- deploying `app.js` alone
  would leave a node that dies on its first `require`.
- `GET /chain/blocks?limit=N` returns only the newest N blocks (max 500); with
  no limit it still returns the whole chain.

### Added
- Read-only chain tools Tala can call: `chain_status`, `chain_recent`,
  `chain_read` and `chain_verify` (`sdk/agent/chain_tools.py`). They report
  node health, the newest blocks, one transaction by id, and an independent
  re-verification of the ledger file; a node that is down is an answer, not an
  exception, and no document body is ever returned.
- Tala's complete natal chart (`python/astrology/natal.py`): born 1977-07-20,
  5:00 pm, Clearwater, Florida (17:00 EDT = 21:00 UT). Adds the Ascendant and
  Midheaven, Placidus houses, signs, essential dignity, retrogrades, chart
  ruler, day/night sect, element and modality balance and natal aspects, and
  shows the summary in the `astro.transits` RPC. Replaces the 12:00 UT
  placeholder hour, which made houses and angles impossible. Every
  date-only persona config now resolves through one `birth_for` helper, so her
  chart no longer depends on which code path read it; a configured
  `birth_hour` of 0 is now honoured instead of becoming noon. The new hour
  changes the engine's label for 112 of the 522 corpus stories, so those
  stories' anchor datetimes were moved (median 2 days) to dates where her real
  chart computes the emotion each already declares; turn text and labels are
  untouched. Groundwork for chart-and-memory-driven topic stance.
- US-origin local model stack with per-machine **inference profiles**
  (`kagura`, `nano`; `python/inference/profiles.json`, `docs/inference-profiles.md`).
  Main chat and tools are gpt-oss-20b Heretic on llama.cpp (harmony format,
  native `tool_calls`, reasoning effort medium for tool turns and low for
  chat, reasoning never shown); Llama 3.1 8B abliterated is the Ollama
  fallback and `gemma3:4b` (`num_ctx` 8192) is vision. A per-turn router picks
  vision for an image, main otherwise, fallback on error or timeout, and a VRAM
  arbiter keeps one big model on the 6 GB GPU at a time. Paths default to D:;
  `TALA_PROFILE` / `python -m python.inference.profiles use <name>` switch.
  No cloud provider is in an automatic chain unless a profile sets `allow_remote`.

### Removed
- Every non-US-origin base-model reference outside the ledger, historical
  benchmark records and the migration that retires them, including the old
  0.5B LoRA adapter and the old vision default (now `gemma3:4b`).
- LoRA is labelled **not implemented** (`lora.implemented = false` in the
  engine status API); Tala no longer queues training jobs that cannot succeed.

### Removed
- The electron-builder distribution format (`npm run pack`/`package:*`, the
  `build` section in package.json, the CI packaging smoke on its output).
  Its `files` allowlist had to be maintained in lockstep with the zip
  bundler's `BASE_EXCLUDES`, drifted anyway, and nothing it produced was
  ever published; the source zips from `scripts/build_bundles.py` are the
  one distribution format. CI's package-smoke now builds the real bundle
  and inspects it for entrypoints and leaked live state.

### Changed
- The OTA updater applies via staged extraction with backup and rollback:
  a corrupt archive leaves the install untouched, and a failed apply
  restores the pre-update files.
- CI runs a dependency audit that actually gates: runtime dependencies at
  moderate, dev tooling at high (the previous "gate" was an `npm install`
  comment — install prints advisories and exits 0).
- The deployed Caddyfile is the repo's again: static site plus `/chain/*`,
  and the `/ws` peer route restored, with 8765/8332 bound to loopback.

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

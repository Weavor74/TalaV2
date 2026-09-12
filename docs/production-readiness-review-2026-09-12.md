# Tala V2 — Production-Readiness Review & Ordered Fix List (Re-Audit)

**Date:** 2026-09-12 · **Version audited:** 0.1.83 · **HEAD:** a21fcc1
**Method:** Four parallel audits — security re-verification, cross-platform sweep, engineering quality/release engineering, and market comparison (Hermes Agent, OpenClaw, Grok Bot). Every code finding was verified by reading current source; file:line references included. This supersedes [production-readiness-review-2026-09-09.md](production-readiness-review-2026-09-09.md).

---

## Verdict

**Maturity: solid beta candidate for a desktop-only release; NOT yet shippable as a federation-enabled (public VPS / P2P) release. The gap has shrunk from "architecture + hygiene + platform" to "five new federation-layer findings and one operator ceremony."**

What changed since 2026-09-09 — all verified in source, not from commit messages:

- **All 7 previous P0 security fixes are present and correctly implemented.** The `p2p.encrypted_payload` RCE is closed with a frozen 5-verb inner allowlist enforced *before* decryption plus structural connection-class gating (server.py:15726-15782, 16513-16515). Enrollment is deny-all until approved, and re-keying requires proof of the old secret (server.py:6484-6560). The unsigned OTA IPC channel is deleted. Web UI token is mandatory on every interface including loopback, with Origin checks and rate limits. Key files are 0600 with self-heal. Rate limiting exists on handshakes, enrollment, bridge, and `/chain/tx`.
- **All 7 previous P1 cross-platform fixes are present and correct** (python resolution, shell declaration, macOS desktop-control backend with Retina scaling, installer/doctor parity, updater extractor pre-check, pyproject packaging). CI now runs a Windows + macOS smoke matrix.
- **Hygiene is genuinely closed as a category:** 2,232 tests green in 80s, eslint + ruff + npm-audit gate CI, size ratchets bite (pins equal current sizes exactly), deps exact-pinned with lockfiles, docs verified accurate, junk files ~90% gone.

What blocks production now:

1. **The operator ceremony is still owed** — the live download zips in `blockchain/public/` still contain the PoA signing key, and they are the *pre-fix* v0.1.83 builds. The public download channel is currently distributing the vulnerable build with a live signing key inside. This is the single most urgent item and it is operational, not code.
2. **Five new security findings (N1–N5)**, all in the federation/updater/chain layer: an E2EE MITM chain via unsigned `webrtc_offer` + unsigned handshake responses + a writable chain peer directory; a PowerShell injection in the updater; a fail-open update-integrity check; and a forgeable/replayable chain payment signature scheme.
3. **Three cross-platform gaps** stand between the code and your stated requirement that every function works on Windows/macOS/Linux: Wayland desktop control, missing `xclip` provisioning, and one cmd.exe shell-dialect divergence.
4. **Unsigned installers** — no Windows code-signing or macOS notarization config exists; a public beta will hit SmartScreen/Gatekeeper walls.

---

## How to read this list

Items are in the order you should fix them. **P0** = do not ship or run a public VPS/federated node until done. **P1** = required for the cross-platform claim and a credible public beta. **P2** = pre-beta hygiene. **P3** = maturity work.

Effort key: **S** = under an hour, **M** = a day-ish, **L** = multi-day. **OP** = operator action, not code.

---

## P0 — Release blockers

### 1. Rotate keys, rebuild, republish (CRITICAL, OP + S)
Verified today: `blockchain/public/tala-{linux,macos,windows}.zip` each still contain `blockchain/data/poa_keys.json` (leaked key `b9360c…`) and `chain.json`, and they are stale pre-fix builds — anyone downloading Tala right now gets the vulnerable build *and* a valid block-signing key. `tests/test_release_bundles_carry_no_secrets.py` is intentionally red on exactly this.
**Do, in order:** (a) rotate the PoA key on the VPS and revoke `b9360c…`; (b) rotate the `.env` Postgres password; (c) rebuild all bundles with `scripts/build_bundles.py` (the SECRET_ENTRY tripwire now guards this); (d) redeploy the VPS; (e) publish the `sha256` map in `version.json` (see #5); (f) confirm the bundle-secrets test goes green. This closes the review's oldest open item and unblocks #5.

### 2. Untrack the release zips from git (HIGH, S)
`git ls-files blockchain/public/` shows all four zips (~33 MB) are committed — meaning the leaked key also lives in git history and every release bloats the repo. Untrack them, gitignore the directory, and serve bundles from the VPS webroot only. History copies are one more reason #1's rotation is non-negotiable rather than optional.

### 3. E2EE MITM chain: unsigned `webrtc_offer` + identity-unchecked `relay.deliver` (HIGH, M)
`handle_p2p_webrtc_offer` (python/harness/server.py:15697-15705) does no signature, contact, or approval check, and `webrtc_manager.py:57-64` registers the data channel under the caller-chosen `peer_username`. The webrtc verbs are relayable (vps_mode.py:68-69), `relay.send` is available to **unapproved** peers (vps_mode.py:100-107), and `relay.deliver` (server.py:15672-15694) never checks that the inner `peer_username` matches `from_username`. A stranger auto-enrolls, relays a forged offer, and the victim binds the attacker's channel as "alice"; outbound routing prefers open WebRTC channels (server.py:5809-5811) and handshakes are E2EE-exempt (server.py:5800), so the next handshake — carrying `shared_profile` PII — goes to the attacker in plaintext.
**Fix:** require an HMAC-signed packet (contact secret) on `webrtc_offer`/`webrtc_answer`; in `relay.deliver`, refuse when inner identity ≠ `from_username`; drop the webrtc verbs from what unapproved senders can relay.

### 4. E2EE key substitution: unsigned handshake responses + anyone-writable chain peer directory (HIGH, M)
`server.py:6746-6748` installs the peer's `e2ee_public_key` from a handshake *response* with no authentication (the inbound direction verifies HMAC at :6802; the response direction verifies nothing). Independently, `POST /chain/peers/register` and `/chain/peers/heartbeat` are unauthenticated and register() **overwrites an existing entry's `endpoint_url`** with no proof of identity (blockchain/src/app.js:1239-1285). The 09-09 fix protected the harness contact table; the analogous hijack survives in the directory: re-point @alice's directory entry, answer the victim's handshake with your own key, read all subsequent "E2EE" traffic.
**Fix:** HMAC-sign the handshake response (over `accepted` + `e2ee_public_key`) with the pair secret and verify at :6746; require proof of the registered signing/wallet key to mutate a directory row.

### 5. Updater: PowerShell injection + fail-open integrity (HIGH, S–M)
Two bugs in the surviving (correct-by-design) update path:
- **Injection (Windows):** electron/main.js:682 takes `params.url` from the renderer; `new URL()` validation (:689-698) accepts `$`, `(`, `)` in paths, then the **raw string** is interpolated into a double-quoted PowerShell script (:736-741, :762). `https://talacore.app/$(Start-Process calc)` passes host pinning and executes at update time — defeating exactly the compromised-renderer threat the pinning was added for. **Fix:** interpolate `parsedUrl.href`, pass the URL as a parameter/env var, single-quote PS literals.
- **Fail-open:** main.js:701-704 proceeds "without integrity verification" when no published hash exists — and the live `version.json` has **no `sha256` map**, so every real update today is protected by TLS-to-pinned-host only. **Fix:** publish the hash map (part of #1e), then make a missing hash a hard refusal.

### 6. Chain payment signatures don't cover the amount — forgery + infinite replay (HIGH, M; or descope)
Payment auth verifies an ed25519 signature over only the caller-supplied `hash` **string** (blockchain/src/app.js:526-546, 1330-1345); the hash is never recomputed against content, `metadata.amount`/`counterparty` are unbound, and duplicate tx ids aren't rejected. The chain is world-readable (`GET /chain`, app.js:1288-1291), so anyone can take a historical signed payment from wallet W, resubmit it with `amount = W's full balance, counterparty = attacker` — the signature still verifies and the balance drains (app.js:468-495). Unmodified payments replay infinitely.
**Fix:** sign a canonical serialization including amount/counterparty/timestamp/nonce; recompute and enforce `hash`; reject duplicate ids. **Or:** scope payments out of the beta and gate `/chain/tx` to admin-only until the scheme is redesigned — if TalaCoin balances are meant to mean anything, this is a blocker.

---

## P1 — Cross-platform completion + beta credibility

### 7. `tester.py` runs agent commands under cmd.exe on Windows (M→S)
sdk/agent/tester.py:162-168 executes `run_cmd` via `subprocess.run(..., shell=True)` — the platform default shell — exactly the divergence the shell_platform work eliminated everywhere else. Reached via the `code.repair` RPC (python/harness/agent_bridge_handlers.py:135-152) with model-authored, typically POSIX-flavored commands.
**Fix:** wrap with `sdk.agent.shell_platform.wrap_command()` like every other execution site.

### 8. Desktop control is dead on Wayland — the default session on modern Ubuntu/Fedora (S for honesty, L for support)
sdk/agent/desktop_control.py:90-109 is xdotool/X11-only (self-documented at :96); pyautogui/Pillow screenshots also fail on pure Wayland. Nothing warns — the failure surfaces only at tool-call time.
**Fix (minimum, do now):** doctor.sh + first-run detection of `XDG_SESSION_TYPE=wayland` with a clear "log into an X11 session for desktop control" message, and note it in INSTALL. **Fix (full, post-beta):** ydotool + xdg-desktop-portal screenshot backend.

### 9. Linux Unicode typing needs `xclip`, which nothing installs (S)
The clipboard-paste fallback (desktop_control.py:268-298) uses pyperclip, which on Linux requires the external `xclip`/`xsel` binary — absent from install-linux.sh (xdotool is installed at :90, no clipboard tool). Non-ASCII `type` actions fail on standard installs.
**Fix:** add `xclip` to install-linux.sh's `install_if_missing` list and a doctor.sh probe.

### 10. Code signing and notarization (M–L, includes OP)
electron-builder has zero win/mac signing or notarization config. A public beta whose installer triggers "Windows protected your PC" / "unidentified developer" loses most users at the door — and the market research below shows signed updates are now table stakes in this category.
**Fix:** minimum — document the bypass in INSTALL; properly — acquire a Windows cert + Apple Developer ID, wire `win.certificateFile` / `mac.notarize` into package.json's build config.

### 11. SSRF + plaintext transport on peer endpoints (M)
- Enrollment stores `packet.sender_endpoint` verbatim (server.py:6474) and `_normalize_peer_endpoint` (server.py:~183-193) accepts any host/scheme including loopback/RFC1918 — the VPS then dials it. **Fix:** restrict schemes, deny loopback/private ranges by default.
- The same normalizer defaults bare hosts to `ws://` — the 09-09 fix covered the server side only; the client still speaks plaintext to non-loopback peers. **Fix:** refuse `ws://` for non-loopback.

### 12. No replay protection on P2P packets or E2EE envelopes (M)
`_verify_packet` (server.py:6366-6370) checks HMAC only — no timestamp/nonce — so captured packets can be re-delivered indefinitely by a relay or observer. The WS handshake already has the right pattern (nonce + skew, ws_auth.py:338-343); apply it to packets.

---

## P2 — Pre-beta hygiene

13. **Persistent logs (S).** Everything goes to stdout → renderer console; a crash-on-startup leaves no artifact to debug a beta user's report. Add a `RotatingFileHandler` under the userData dir next to `_configure_logging` (server.py:110) and have doctor/self-test print its path.
14. **Root utility-script residue (S).** Seven scripts remain at repo root; `fetch_logs.py`/`get_logs.py` query a `system_logs` table nothing writes — dead. Delete the dead ones, move live ones (`clean_db.py`, `migrate_all.py`, `query_db.py`, `reindex_nodes.py`, `scrub_paths.py`) into `scripts/`.
15. **Separator-sensitive vault check (S).** server.py:1669 `background_file.startswith("vault/")` — a stored `vault\x.html` bypasses VaultManager on Windows. Normalize-then-compare (pattern exists at server.py:13892).
16. **MCP args parsing eats Windows paths (S).** python/harness/mcp_manager.py:305-306 uses POSIX `shlex.split()` on user MCP `args` (`C:\Users` → `C:Users`). Use `posix=(os.name != "nt")` or require list-form args.
17. **macOS offline TTS is silent (S).** python/harness/voice.py:100-126 — pyttsx3's nsss driver writes AIFF labeled `audio/wav`; Chromium can't decode it. Save as `.aiff`/transcode, or mark the backend unavailable on darwin.
18. **Decomposition momentum (M, recurring).** server.py is pinned at exactly 17,758 lines / 272 handlers; renderer.js at 16,557 with 41 JS tests. The ratchet stops growth; nothing schedules shrinkage. Adopt "extract one domain per release, drop the pin" — the jsonrpc/agent_bridge/system_info extractions proved the pattern.
19. **Missed 0600 writer (S).** electron/main.js:637 writes `vault/.keys/harness_admin_secret.txt` without `mode: 0o600` — the one key writer the 09-09 fix missed (Python self-heals only on later read).

---

## P3 — Maturity (post-beta)

20. Timing-safe admin-secret compare in blockchain/src/app.js:333, 1308 (`===` today; Python side already uses `compare_digest`).
21. `p2p.resolve_node_reference` returns local absolute `source_path` to peers (server.py:7206) — filesystem-layout leak.
22. webui.py:140-145 secret-path carve-out is case-sensitive — `Vault/...` bypasses it on case-insensitive volumes (root confinement still holds). Case-fold the check.
23. WebRTC error handler returns raw `str(exc)` to peers (server.py:16389); align with the sanitized WS path.
24. No uniqueness on contact `wallet_address` (server.py:6583-6587) — ambiguity in wallet-keyed lookups (server.py:6316).
25. `ws_auth._HANDSHAKE_HITS` never prunes per-IP keys (ws_auth.py:191-212) — slow memory growth under scanning.
26. `.workflow_state.json` opened text-mode without `encoding="utf-8"` (sdk/agent/tools.py:3193, 3286).
27. `sdk/gateway/resolver.py:28` `resolve_command()` is dead code — remove or wire into MCP stdio spawning.
28. Windows secret files aren't owner-only (0o600 is a POSIX no-op); add `icacls`-based ACLs for parity on multi-user machines.
29. Process parity: `scripts/upload_vps.ps1` has no POSIX counterpart; the full pytest+Postgres CI job is Linux-only — Python behavioral regressions on Windows/macOS can ship untested. Add at least a Windows pytest job for the fast tier.
30. Renderer test growth (41 jest tests vs 16.5k lines — grow with every extraction), broaden eslint rules (config header already promises this), consider `asar: true` with unpacked python/ for tamper-resistance, silence the 13 pytest warnings (wrong `@pytest.mark.asyncio` in test_peer_approval_gate.py:75, test_request_notification.py:134), and add an opt-in "attach diagnostics" bundle (doctor output + the #13 log file).
31. Strategic (unchanged from 09-09): mobile/companion story; stable/LTS release channels.

---

## Where Tala sits vs. Hermes, OpenClaw, and Grok Bot (September 2026)

| Dimension | Hermes Agent (Nous) | OpenClaw (Foundation) | Grok Bot (xAI) | **Tala 0.1.83** |
|---|---|---|---|---|
| License / model | MIT OSS, self-hosted | MIT OSS, self-hosted | Proprietary SaaS ($120–300/mo) | Local-first desktop app |
| Adoption | 243K stars (Sept 8) | ~388K stars (Aug 26) | "Thousands of orgs" (unverified) | Pre-release |
| Platforms | macOS (Apple Silicon only), Win 10/11 (WSL2 advised), Linux, Android | Win/macOS/Linux gateway; messaging apps as UI | Cloud agent; macOS/Win/Linux/iOS clients | Win/macOS/Linux native; **Wayland gap (#8)** |
| Local models / offline | Best-in-class one-click local setup; fully offline possible | Cloud LLMs typical; local possible | None — cloud only | Fully local, offline-first |
| Desktop control | Host shell/files; not GUI computer-use | Shell/files/browser on host | Full computer-use **on its cloud VM only** | Observe-then-act GUI control on the user's own machine — unique quadrant |
| Multi-device sync | None built-in (top-requested gap, issue #20510) | None native; cloud shared sessions | Implicit via cloud account | E2EE P2P sync, no cloud broker — differentiator **once N1/N2 land** |
| Security track record | ~6 CVEs + ignored disclosure; audit found 4 Crit/9 High in defaults; opt-in sandbox, YOLO mode | **~138 CVEs in 5 months**, 512-finding audit, 500K exposed instances, 1,400+ malicious hub skills, Microsoft/Belgium/China advisories | No public CVEs; own docs warn Bots share one VM, "not a security boundary"; regulator investigations | 7/7 audited P0s fixed structurally; deny-all enrollment; mandatory tokens; N1–N5 open in federation layer |
| Signed updates | SHA-256 + optional cosign for one component; no full story | "Inspect before replacing"; no code-signing story | N/A (cloud-managed) | Pinned-host + sha256 design exists but fail-open + unpublished hashes (#5); installers unsigned (#10) |
| Audit logs | Not a feature | Not a feature | Enterprise tier only (Sept 3) | Opportunity: local tamper-evident action log would beat all three at $0 |

**What "production ready" means in this category in 2026** (written, effectively, by OpenClaw's Jan–Apr meltdown): secure-by-default installs with auth on and secrets encrypted; sandboxing as the default not opt-in; deterministic per-action permission prompts (LLM-judged auto-approval is now considered an anti-pattern); named prompt-injection defenses; a vetted skill supply chain; **signed, verifiable updates** (an open gap for both OSS rivals); audit logs/action recording; and 72-hour disclosure turnarounds.

**Positioning:** the "secure by default, local, yours" seat is vacant. Hermes has the best security *docs* but loose defaults; OpenClaw has the scar tissue; Grok Bot is cloud-captive and expensive. Tala's honest wedge is (a) true local observe-then-act desktop control + local models, (b) E2EE P2P multi-device sync with no cloud broker — the top-requested Hermes gap — and (c) Grok-Enterprise-grade auditability for free, locally. Tala cannot win on ecosystem scale or velocity; it can win the buyers the incumbents' security records disqualified (note Microsoft's and China's OpenClaw restrictions created exactly this market). **Every claim in that pitch survives scrutiny only after P0 items 1–6 land** — "we ship what OpenClaw learned the hard way" dies instantly if the published bundles still contain a signing key.

---

## Recommended beta scoping

- **Defensible today (after P0 #1–2 only):** desktop-only beta — loopback WS, web UI token-gated, no public VPS, payments off. The local surface is genuinely hardened.
- **Federation-enabled beta:** additionally requires #3–#6 (E2EE MITM chain, updater, chain payments) and #11–#12.
- **"Every function cross-platform" claim:** requires #7–#9 (with #8 at minimum as a loud doctor warning); #15–#17 round out the long tail.

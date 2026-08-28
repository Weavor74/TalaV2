# UI Audit — Findings Backlog

Running list of everything found while mapping Tala's UI screen by screen, so that
the map can eventually be authored as HTML-NN nodes and Tala can self-navigate.

**Nothing in this document has been addressed.** It is a punch list only.

- Started: 2026-08-23
- Method: Steven supplies a screenshot and names the regions; findings are verified
  against `renderer/index.html`, `renderer/renderer.js`, and the Python harness before
  being recorded here.
- Status vocabulary: `open` · `needs-decision` · `answered` · `done`
- Verdict vocabulary for controls: **works** · **partial** · **dead**

---

## Screen coverage

| Screen | View id | Mapped | Node authored |
|---|---|---|---|
| Landing / Neural Nodes | `view-neural-node` | yes (2026-08-23) | **ui-view-neural-node** |
| Web Browser | `view-browser` | yes (2026-08-23) | **ui-view-browser** |
| Research Library | `view-research` | yes (2026-08-23) | **ui-view-research** |
| Blockchain Ledger | `view-blockchain` | yes (2026-08-23) | **ui-view-blockchain** |
| Agent Builder | `view-agent-builder` | yes (2026-08-23) | **ui-view-agent-builder** |
| Mission Control | `view-mission-control` | yes (2026-08-23) | **ui-view-mission-control** |
| Vault Directory | `view-vault` | yes (2026-08-23) | **ui-view-vault** |
| Contacts Directory | `view-contacts` | yes (2026-08-23) | **ui-view-contacts** |
| System Settings | `view-settings` | yes (2026-08-23) — all 18 panels | **ui-view-settings** |
| Editor | `view-editor` | yes (2026-08-23) | **ui-view-editor** |
| Logs | `view-logs` | yes (2026-08-23) | **ui-view-logs** |

`view-editor` and `view-logs` have no sidebar entry, and UI-012 explains why for each: the editor
is the centre pane (UI-116), and the logs view is vestigial (UI-117). **All 11 views and all 18
settings panels are now mapped.**


### Settings sub-panel coverage

Settings is audited one panel at a time (Steven's method: he names a panel, sends one or more
screenshots for it, since several scroll beyond one screen).

Panels are selected by **`data-panel`**, not `data-tab`. Nav is `#settings-tab-nav`
([index.html:422](../renderer/index.html#L422)). There are **18** panels, though the source
comment above them still says "Unified 15-tab navigation" — stale by three.

| # | Panel (`data-panel`) | Nav label | HTML-NN node | Mapped |
|---|---|---|---|---|
| 1 | `inference` | INFERENCE | `settings-inference` | **yes (2026-08-23)** |
| 2 | `agent` | AGENT | `settings-agent` | **yes (2026-08-23)** |
| 3 | `identity` | USER PROFILE | — | **yes (2026-08-23)** — all 9 sub-sections |
| 4 | `workflows` | WORKFLOWS | `settings-workflows` | **yes (2026-08-23)** |
| 5 | `guardrails` | GUARDRAILS | `settings-guardrails` | **yes (2026-08-23)** |
| 6 | `search` | SEARCH | `settings-search` | **yes (2026-08-23)** |
| 7 | `system` | SYSTEM | `settings-system` | **yes (2026-08-23)** |
| 8 | `git` | .GIT | `settings-git` | **yes (2026-08-23)** |
| 9 | `storage` | STORAGE | `settings-storage` | **yes (2026-08-23)** |
| 10 | `database` | DATABASE | `settings-database` | **yes (2026-08-23)** |
| 11 | `runtime` | RUNTIME | `settings-runtime` | **yes (2026-08-23)** |
| 12 | `auth` | — | `settings-auth` | no |
| 13 | `firewall` | — | `settings-firewall` | no |
| 14 | `architecture` | — | — | no |
| 15 | `channels` | CHANNELS | — | **yes (2026-08-23)** |
| 16 | `logging` | LOGGING | — | **yes (2026-08-23)** |
| 17 | `plugins` | — | — | no |
| 18 | `about` | ABOUT | — | **yes (2026-08-23)** |

Two mismatches to resolve as we go:

- **`settings-code-registry` node exists with no matching panel.** Either the panel was renamed or
  the node is orphaned.
- **Five panels have no node**: `architecture`, `channels`, `logging`, `plugins`, `about` — plus
  `identity`, which likely shares the profile/vault nodes rather than a settings node.

Nav labels confirmed from the live screenshot, in order: INFERENCE · AGENT · USER PROFILE ·
WORKFLOWS · GUARDRAILS · SEARCH · SYSTEM · .GIT · STORAGE · DATABASE · RUNTIME · PLUGINS · AUTH ·
FIREWALL · CHANNELS · LOGGING · ARCHITECTURE · ABOUT. All 18 are present in one row.

Naming convention for this audit: `settings.<panel>` — e.g. `settings.inference` covers the whole
INFERENCE panel however many screenshots it takes.

---

## A. Confirmed defects

### UI-001 — Status bar "Tokens" shows consumption, not wallet balance
**Severity: high · Status: fixed (2026-08-28) — resolved as (a)**

Fixed the way the finding recommended, since it matches the stated intent: the status bar shows the
wallet balance, and consumption keeps its own place rather than being deleted.

`harness.health` now returns three fields where it returned one — `token_balance`, `token_mode`
(`ghost` / `real` / `unreachable`) and `tokens_used`. The status bar reads the balance and is
labelled **Balance**; consumption sits beside it as **Used**.

**A second conflation underneath, found while fixing this.** `get_wallet_balance()` returns the
ghost sentinel *both* when tokens really are unlimited **and** when the chain could not be reached,
so "you have infinite tokens" and "nobody could be asked" were the same answer. Had UI-001 been
fixed by pointing the display at that function, the status bar would have cheerfully reported
unlimited tokens every time the chain was down. `wallet_state()` separates the three cases, and
`unreachable` carries no balance at all — an unknown balance is not a balance.

The renderer no longer infers the mode from the magic number either; it is sent explicitly.

Live check: `used=8,488,269` (the reference screenshot's figure, now correctly labelled),
`balance=999999.0`, `mode=ghost`.

**Original finding.**


The status bar number (`8,488,269` in the reference screenshot) comes from
`harness.health` at [server.py:3651](../python/harness/server.py#L3651):

```sql
SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM token_ledger
```

That is cumulative LLM tokens consumed for the life of the database. Steven's stated
intent is that this field shows **blockchain tokens** — i.e. `wallet.ai_token_balance`,
which is currently only reachable via the `/wallet` slash command
([renderer.js:7406](../renderer/renderer.js#L7406)).

Two independent signals that this is a wiring bug rather than a naming quibble:

1. Python sends the field as `tokens_used` (consumption); the renderer consumes it with
   `setTokenBalance()` ([renderer.js:6034](../renderer/renderer.js#L6034)) — a balance display.
2. `setTokenBalance` contains `if (n === 999999) → "Ghost Tokens (∞)"`. `999999.0` is the
   ghost-mode sentinel returned by `get_balance()` in
   [token_counter.py:459](../python/harness/token_counter.py#L459) — a *wallet* function.
   The display was written to receive `get_balance()`; `harness.health` feeds it a lifetime
   sum instead.

**Decision required.** Either:
- **(a)** re-point `harness.health` at `get_balance()` so the status bar is a wallet balance
  (this also makes the ∞ branch reachable in local mode), or
- **(b)** keep it as a usage meter and fix the cosmetics instead — rename `setTokenBalance`,
  change the label from `Tokens:` to something unambiguous, resolve UI-002 and UI-003.

Recommendation on file: **(a)**, because it matches both stated intent and the dead ∞ branch.

### UI-002 — `Ghost Tokens (∞)` branch is unreachable
**Severity: medium · Status: fixed (2026-08-28)**

Resolved by UI-001(a), as predicted. The branch fired on `n === 999999` while being fed a lifetime
consumption total, which is why it had never once run. The mode now arrives as a field rather than
being sniffed from a sentinel, so "unlimited" is reachable — and, more to the point, it can no
longer be shown when the real situation is that the chain is unreachable.

**Original finding.**


[renderer.js:6036](../renderer/renderer.js#L6036). Fires only when the value is exactly
`999999`. Fed a monotonically growing lifetime sum, it can realistically never trigger.
Resolves automatically under UI-001(a); becomes dead code to delete under UI-001(b).

### UI-003 — Sidebar token bar hardcodes a 1,000,000 maximum
**Severity: low · Status: fixed (2026-08-28) — the finding was understated**

Not a miscalibrated bar: **there is no bar**. `#token-bar` and `#sidebar-token-balance` are written
to on every health tick and appear nowhere in `index.html`, so the 1,000,000 maximum was scaling an
element that does not exist. Along with `#tokens-used`, that is three selectors quietly writing into
nothing. All three are gone; consumption now has a real element.

**Original finding.**


[renderer.js:6047](../renderer/renderer.js#L6047) scales the bar against an assumed max of
1,000,000. The observed value is 8,488,269 — 8.5× over — so the bar is pinned at full and
conveys nothing.

### UI-004 — `MOOD` button is cosmetic
**Severity: medium · Status: FIXED 2026-08-23**

> **Fixed.** Button and the catch-all stub handler both removed from the chat header.

`btn-chat-mood` ([index.html:2981](../renderer/index.html#L2981)) has no dedicated handler.
It falls through to the generic `.chat-action-btn` listener at
[renderer.js:4894](../renderer/renderer.js#L4894), which toggles a CSS class and writes:

```
Agent Chat modifier 'MOOD' set to ENABLED
```

The id appears **only** in `index.html` — nothing reads the toggled state. The button lights
up and nothing else happens. Given the astro-emotion subsystem exists, this is likely the
intended surface for it and was never connected.

### UI-005 — `MD` (Render Markdown) button is cosmetic
**Severity: medium · Status: FIXED 2026-08-23**

> **Fixed.** Button and the catch-all stub handler both removed from the chat header.

`btn-chat-md` ([index.html:2982](../renderer/index.html#L2982)). Same generic handler, same
absence of any consumer. Markdown rendering in chat does not key off it.

### UI-006 — `JSON` (View JSON State) button is cosmetic
**Severity: medium · Status: FIXED 2026-08-23**

> **Fixed.** Button and the catch-all stub handler both removed from the chat header.

`btn-chat-json` ([index.html:2983](../renderer/index.html#L2983)). Same generic handler, same
absence of any consumer.

> **UI-004/005/006 share one root cause.** The delegated handler at
> [renderer.js:4894](../renderer/renderer.js#L4894) explicitly skips the four buttons that have
> real behaviour (`rp`, `hybrid`, `assistant`, `tts`) and gives *everything else* a
> toggle-and-log stub. Any future `.chat-action-btn` added to the header inherits the same
> silent no-op. Worth fixing the pattern, not just the three buttons.

---

## A2. Confirmed defects — Web Browser view

**Stated intent: "Tala is supposed to have full control of this web browser."**
She has full control of *a* browser. It is not the one on screen.

There are two entirely disconnected browser stacks:

| | Stack A — Playwright | Stack B — the visible webview |
|---|---|---|
| what | `chromium.launch(headless=True)`, a separate invisible process | `<webview id="browser-iframe">` in `view-browser` |
| where | [sdk/agent/web_browser.py](../sdk/agent/web_browser.py) | [index.html:278](../renderer/index.html#L278) |
| Tala's tools | 6, all registered and functional | none |
| user can see it | **no** | yes |

Stack A is healthy: registered at [tools.py:4514](../sdk/agent/tools.py#L4514), `playwright` present in
`.venv`, and the chromium binaries are actually downloaded (`~/AppData/Local/ms-playwright`
contains `chromium-1161`, `chromium-1234`, headless shells, ffmpeg, firefox, webkit). No
dependency drift here. Tools: `browser_navigate`, `browser_read`, `browser_click`,
`browser_type`, `browser_screenshot`, `browser_close`.

The problem is purely that Stack A and Stack B never meet. Everything Tala does happens in a
browser Steven cannot watch, while the on-screen browser sits idle.

### UI-013 — Tala's browser tools drive a headless browser, not the visible one
**Severity: high · Status: open**

The six working tools all operate on the Playwright instance. None of them can navigate, read,
click, or type in the `<webview>`. Watching the Web Browser view tells the user nothing about
what Tala is doing.

Note `webviewTag: true` is set at [main.js:247](../electron/main.js#L247), and an Electron
`<webview>` supports `loadURL` / `executeJavaScript` / `capturePage`. The visible browser is
fully drivable — it simply isn't driven.

### UI-014 — `browser.get_dom` is a stub that reports success
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** The handler now reads the live webview's DOM through a real request/response
> bridge (`python/harness/ui_bridge.py`) and a renderer listener that answers. Every
> outcome is distinguishable: no window open, no handler listening, no element matching
> the selector, or a result. Headless returns `_err` rather than `_ok` with an `error`
> key inside it, which was the same lie in a smaller font.

The RPC handler retrieves no DOM and broadcasts nothing. Its entire non-headless body is:

```python
return _ok(req_id, {"status": "broadcast_sent", "note": "DOM retrieval requires IPC callback setup."})
```

It returns `_ok`, so any caller checking for success sees success. This is the
`status: failed read as success` pattern again — a silent no-op wearing a success response.

### UI-015 — `browser.execute_js` broadcasts to a channel with no listener
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** The listener exists now, and the missing result path with it. `ui_bridge`
> attaches a `request_id`, the renderer evaluates the script and calls the new
> `ui.bridge_result` RPC, and the pending future resolves. A thrown script arrives as a
> failure; a renderer that never answers times out rather than hanging the harness; and
> zero connected clients fails immediately rather than waiting out the timeout to lie.

`handle_browser_execute_js` calls `broadcast_notification("browser.execute_js", {"code": code})`
and returns `{"status": "broadcast_sent"}`. But the renderer listens only for
`browser.navigate` ([renderer.js:7147](../renderer/renderer.js#L7147)) — there is **no**
`browser.execute_js` listener anywhere in `renderer/`. The code is sent into the void and the
call reports success.

Second defect in the same handler: it is fire-and-forget. Even with a listener added, no result
path exists, so Tala would never see what the JS returned. Compare `browser_read` in Stack A,
which does return page content.

### UI-016 — Control of the visible webview is not exposed as tools
**Severity: high · Status: open**

`browser.navigate`, `browser.get_dom`, and `browser.execute_js` are JSON-RPC methods only
([server.py:11084-11085](../python/harness/server.py#L11084)). No `@register_tool` wraps any of
them — a search across `sdk/` for `get_dom` / `execute_js` / `browser.navigate` returns nothing.
Tala cannot call them even if they worked.

The one capability that genuinely functions end-to-end — `browser.navigate` → renderer listener
→ webview — is fired from exactly one place: `research.search`
([server.py:8640](../python/harness/server.py#L8640)), which points the visible browser at a
DuckDuckGo results URL as a side effect of searching. Tala has no way to send the visible
browser to an arbitrary URL on purpose.

### UI-017 — `READY` badge is static decoration
**Severity: low · Status: fixed (2026-08-28)**

The badge reports the webview: `LOADING`, `READY`, `FAILED`, `CRASHED`, wired to
`did-start-loading`, `did-stop-loading`, `did-fail-load` and `crashed`, with colour carrying the
state. `ERR_ABORTED` (-3) is ignored, since it fires on ordinary navigation away from a page.

It was styled markup with no JavaScript anywhere in `renderer/`, reading READY while a page was
loading and while a page had failed. A status indicator that cannot indicate is worse than none,
because it is believed.

**Original finding.**


`<span class="browser-status-ready">READY</span>` at
[index.html:275](../renderer/index.html#L275) has no JS anywhere in `renderer/` — it is styled
by [styles.css:1246](../renderer/styles.css#L1246) and never changes. It reads as a live status
indicator and always says READY, including while a page is loading or has failed.

---

## A3. Confirmed defects — Research Library ("Evidence Browser")

**Steven's label:** *"This is the research view, it is meant to work like NotebookLM. One of the
key features is that it will be able to use the search engine that the user adds with a search
API in a later tab."*

This screen is the best-built one audited so far. The named key feature is genuinely wired
end-to-end: the `Search Engine` dropdown is populated at runtime from user-configured providers
via `search.get_providers` ([renderer.js:3773](../renderer/renderer.js#L3773), called from
[renderer.js:252](../renderer/renderer.js#L252)), the selection is passed as `search_provider_id`
through `research.unified_search`, and `SearchManager.query()` honours it
([search_manager.py:121](../python/harness/search_manager.py#L121)).

The defects below are all in the **provider execution path** — the part that decides whether the
user's own API key is actually what answers the query.

### UI-018 — A failing search provider silently degrades to a DuckDuckGo scrape
**Severity: high · Status: FIXED 2026-08-23 · Direction set by Q-9 — failure must be visible**

> **Fixed.** A non-200 is now recorded as a provider failure with a readable reason
> ("HTTP 401 (the API key was rejected)"), and so are timeouts, non-JSON bodies, and 200s
> carrying no usable rows. `SearchManager.query_detailed` returns a `SearchOutcome` saying
> which provider answered, whether the result came from the crawler, and what failed on
> the way. `query()` still returns a bare list, so no existing caller broke.

In `SearchManager.query`, every provider branch is shaped as:

```python
response = await client.post(endpoint, json=payload)
if response.status_code == 200:
    ...
    return [...]
```

**A non-200 response is not an exception.** On 401 (bad key), 403, 429 (rate limit), or 500, the
branch simply falls off the end without returning and without raising. The `except` clause never
fires, so the "trying next provider" log never prints. The loop exhausts and hits:

```python
logger.info("No active search providers succeeded. Falling back to direct web crawler.")
return await crawl_web(query)
```

The user gets a full page of plausible DuckDuckGo-scraped results and **no indication whatsoever**
that their configured, possibly paid, search API was never used. Because nothing raised,
`errors["web"]` in `unified_search` stays empty and the red error banner never renders.

This is the single most damaging finding for the stated feature: the failure mode of "my search
API isn't working" is *indistinguishable from success*.

### UI-019 — The `/search` substring misroutes most commercial search APIs
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** Dispatch is by host, not by substring: `_provider_kind` recognises Tavily,
> SerpAPI, Brave, Google CSE, Bing and SearXNG, and falls back to generic. Each gets the
> auth it actually wants — Brave its `X-Subscription-Token`, SerpAPI its `api_key`
> parameter, Bing its `Ocp-Apim-Subscription-Key` — and each is read from the right key
> (`organic_results`, `web.results`, `items`, `webPages.value`). SearXNG is now matched by
> name or an explicit `format=json`, never by a bare `/search` path.

Provider dispatch is by substring match on the endpoint
([search_manager.py:175](../python/harness/search_manager.py#L175)):

```python
elif "format=json" in endpoint.lower() or "/search" in endpoint.lower():
    # ...SearXNG handling: ?format=json, Authorization: Bearer, reads data["results"]
```

Almost every commercial search API's endpoint contains `/search`, so they are all captured by the
SearXNG branch and never reach the generic JSON handler below it that would handle them correctly:

| Provider | Endpoint | Routed to | Result |
|---|---|---|---|
| SerpAPI | `https://serpapi.com/search` | SearXNG | reads `data["results"]`; SerpAPI returns `organic_results` → empty |
| Brave Search | `https://api.search.brave.com/res/v1/web/search` | SearXNG | needs `X-Subscription-Token`, gets `Authorization: Bearer` → 401 |
| any self-hosted API at `/search` | — | SearXNG | wrong auth + wrong response key |

Tavily escapes only because it is matched by the earlier `"tavily.com" in endpoint` branch.

This is the substring intent-gate pattern again, and it compounds UI-018: the misroute produces a
non-200 or an empty list, which then degrades silently to the crawler. **Together these two mean a
user can add Brave or SerpAPI, see results appear, and never learn their provider is unused.**

### UI-020 — Provider failures never reach the UI, which already has a banner for them
**Severity: medium · Status: FIXED 2026-08-23 · Direction set by Q-9 — failure must be visible**

> **Fixed, and it needed no renderer change** — the banner always worked, nothing fed it.
> `unified_search` now populates `errors["web"]` from the outcome. Two additions beyond
> the finding: crawler rows score below provider rows so they cannot outrank the real
> thing, and each web result carries the engine that produced it, so the badge reads
> `BRAVE` or an amber `CRAWLER` instead of a uniform `WEB`.

`unified_search` returns an `errors` dict and the renderer renders each entry as a red banner
([renderer.js:3838](../renderer/renderer.js#L3838)). The mechanism works. But `SearchManager`
reports every provider failure to `logger.warning` and swallows it — non-200s do not even reach
the logger. Nothing populates `errors["web"]` unless the whole call throws.

The diagnostic surface exists and is simply not fed.

### UI-021 — Distilled nodes are timestamped, so re-distilling duplicates
**Severity: low · Status: open**

`saveSelectedAsNode` writes to `nodes/concept-${slug}-${Date.now()}.html`. Distilling the same
source twice produces two nodes rather than updating one. Related to the learning-node junk
problem already documented elsewhere — though note this path is user-initiated, so it does not
need the `assess_learning_quality` gate that the autonomous path does.

Minor label inconsistency in the same control: the HTML ships the button as
`Save Selected as Neural Node`, the JS resets it to `Save to Node` when nothing is checked, and it
becomes `Distill Collection (N)` when results are checked. Three names for one button.

### UI-022 — No way to flag a research result into chat
**Severity: medium · Status: open · Source: Q-8**

Per Steven, grounded Q&A over sources is **chat's job**, not this view's — so the absence of an
"ask these sources" panel here is by design, not a gap.

What is missing is the handoff. A result in the Evidence Browser can be viewed
(`memory.browse`) or distilled into a node (`research.distill_to_node`), but there is no control
that pushes a selected result into the chat panel as context to ask about. The two halves of the
NotebookLM workflow — *find the sources* and *ask questions of them* — currently have no
connection between them.

Steven's framing: *"Chat is supposed to cover it but maybe we need something for flagging in chat."*

Design not yet decided. Open questions when this is picked up: does flagging pin the source for
the rest of the session, or attach to a single turn? Does it carry the snippet or the full
`fullContext`? Does Tala cite it back?

---

## A4. Confirmed defects — Blockchain Ledger

**Steven's label:** *"Blockchain ledger. This is supposed to track the chain for both Ghost chain
and Real Chain. Ghost chain essentially is non-value other than tracking transaction locally. But
the real chain only happens when connected to the internet. I think there should be a way to see
the difference in the two."*

Evidence gathered from `blockchain/data/chain.json` (2,714 blocks), `blockchain/src/app.js`, and
`BlockchainViewer` at [renderer.js:6422](../renderer/renderer.js#L6422).

**There is one chain, and it is entirely the local one.** The Ghost/Real split does not exist in
the data model, the node server, or the UI. What exists is a naming convention on transaction ids.

### UI-023 — Ghost/Real is a runtime mode that is never recorded anywhere
**Severity: high · Status: fixed (2026-08-28)**

Ghost and Real are now two chains split by audience, in
[`python/harness/chains.py`](../python/harness/chains.py), with the four open questions answered by
Steven and settled in code.

**What goes where.** `is_external()` decides, in three passes: a declared local kind is local
whatever else it carries; a declared external kind is external; otherwise a *real* counterparty
makes it external. An unrecognised kind with nobody on the other end is Ghost, because the failure
modes are not symmetric — a local transaction wrongly published cannot be unpublished.

**The counterparty field is not the discriminator it looked like.** All 2,748 existing transactions
carry one — 2,747 against `blockchain-ledger` and the genesis `mint` against `tala-network` — and
every one is a placeholder naming *the chain itself*, not a second party. Reading them literally
would have published the machine's entire local history to a chain everybody can read. They are
listed in `PLACEHOLDER_COUNTERPARTIES` and classify as Ghost.

**The transaction `id` is a document identity, not a transaction identity.** The first cut of
`chain_ledger` keyed on it and silently collapsed 2,748 entries into 185 — `settings-workflows`
alone appears 174 times, once per re-anchor. The table now uses a synthetic `entry_id` with `tx_id`
kept as what it actually is.

**The four answers, as built.**

| Question | Answer | Where |
|---|---|---|
| An external interaction while offline | Queued locally, written on reconnect | `chain_ledger.pending`, `chains.flush()`, `server._real_chain_flush_loop()` |
| Does Ghost mirror Real | Ghost records everything; Real is a strict subset | `record()` always writes Ghost |
| One shared Real chain or per-peer | One shared chain | `chain.set_endpoint`, posting to the existing `/chain/tx` |
| The existing 2,748 blocks | The Ghost chain, kept as-is; Real starts empty | `scripts/backfill_chain_ledger.py` |

**The record now exists.** `token_counter` computed `use_ghost_tokens` and sent the answer to a
`logger.debug` line; it now calls `chains.record(..., external=...)`, passing the classification
explicitly because paying yourself for your own resources on your own machine is a payment that is
not an external interaction and only that call site knows the difference. `anchor.py` — the path
that wrote all 2,747 `node-create` transactions — and `settings_nn.py` index as Ghost via
`index_sync()`, keeping their own POST rather than being rewritten async.

**Visible.** `chain.summary` and the Blockchain view show the two chains side by side with a
per-kind breakdown and the pending queue, answering Steven's *"I think there should be a way to see
the diffrence in the two."* A flush that cannot connect reports how many are still queued and why,
rather than looking drained.

Backfilled against the live database: 2,748 Ghost entries over 185 distinct documents, Real empty.
38 tests in [`tests/test_chains.py`](../tests/test_chains.py).

**Reconcile (2026-08-28).** `chains.reconcile()` closes the read-back gap: it pulls
`GET /chain/blocks` from the shared chain, verifies it here, indexes what other peers appended, and
confirms our own queued transactions that the chain already holds. `chain.reconcile` and
`chain.verify` are the RPCs; the reconnect loop reconciles in the same pass as the flush, since that
is the moment the gap is worth closing. `origin` separates what this machine wrote from what came
back off the chain — otherwise "everything on the Real chain" and "everything I put on the Real
chain" are the same query and they are not the same question.

**It pulls; it never pushes.** The Node server's `POST /chain/reconcile` used to overwrite the
receiving chain whenever the incoming one was as long or longer; that is fixed in UI-124, and
reconcile still pulls, since a pull is what reading the chain back requires.

**Verified here, not taken on trust.** [`chain_verify.py`](../python/harness/chain_verify.py)
recomputes block hashes, checks the `previousHash` links and verifies Ed25519 signatures, byte-for-byte
compatible with `Blockchain.isValid()` in `blockchain/src/app.js` — a chain one accepts and the other
rejects would be a split nobody could diagnose. `GET /chain/status` returns an `isValid` the node
computed *about itself*, which answers nothing when the question is whether to trust that node.
Checked against all 2,748 local blocks: 0 bad hashes, 0 broken links, 2,747 valid signatures and the
unsigned genesis. A chain that fails is rejected whole — a chain with the bad blocks skipped is not
a chain.

**A third key mistake, same shape as the first two.** `entry_id` was the primary key on its own, but
an external event is written to both chains and keeps one entry id across them — which is precisely
what makes a Ghost row and its Real counterpart recognisable as the same event. So the Ghost row
swallowed the Real one and `ON CONFLICT DO NOTHING` discarded it without a word: reconcile reported
learning four transactions from a peer and wrote none of them. The key is now `(chain, entry_id)`,
and `index()` no longer reports a zero-row insert as written. Both are regression-tested.

**Still not done.** Reconcile pulls on reconnect and on demand, not on a schedule, so a long-running
online session learns about peers when it next flushes rather than continuously.

**Original finding.**

**Severity: high · Status: was open**

The Ghost/Real distinction **is implemented** — as a *connection mode*, not as a property of a
transaction. There is a full `chain.*` RPC family
([server.py:11086-11090](../python/harness/server.py#L11086)):

| Method | Meaning |
|---|---|
| `chain.stay_local` | Ghost mode — per README, "all interactions use ghost tokens... never touches the blockchain" |
| `chain.connect_vps` | connect to a remote chain (this is what the `Connect VPS` button drives — answers Q-4) |
| `chain.auto_connect` | derive and connect automatically |
| `chain.disconnect` | drop back to local |
| `chain.connection_get` | read current mode |

`token_counter` computes the mode per call
([token_counter.py:312](../python/harness/token_counter.py#L312)):

```python
use_ghost_tokens = system_is_local or resource_is_own
```

**The problem is that this determination is then discarded.** The very next statement is the only
place the result is expressed:

```python
logger.debug("Token mode: %s ...", "ghost" if use_ghost_tokens else "real", ...)
```

A `logger.debug` line. Nothing persists it:

- **`token_ledger`** columns are `id`, `session_id`, `prompt_tokens`, `completion_tokens`,
  `cost_usd`, `model`, `created_at` ([db.py:103](../python/harness/db.py#L103)) — **no mode column**.
- **Chain transactions** carry `id`, `type`, `hash`, `creator`, `counterparty`, `timestamp`,
  `document`, `metadata`, `signature`, `public_key` — **no mode field**. Across all 2,714 blocks
  the only `type` values are `node-create` (2,713) and `mint` (1).

So Steven's ask — *"a way to see the difference in the two"* — is currently impossible **not
because the modes don't exist, but because neither store records which mode was active.** Once a
transaction is written, the information is gone.

The only surviving trace is an id naming convention: 694 transactions are named
`ghost_commit_<uuid>`, all from creator `tala-agent-loop`, all `document_type: thought-state`.
That is a string prefix, not a field — nothing queries or filters on it.

**Sequencing constraint:** this is a persistence change first (add the mode to `token_ledger` and
to the transaction schema), a UI change second. Building a Ghost/Real toggle on the current
schema would have nothing to filter on.

### UI-024 — No transaction has ever been written in Real mode
**Severity: high · Status: fixed (2026-08-28)**

Correct, and it stays correct until this install actually interacts with somebody else — which is
the designed behaviour, not a defect. What was missing was the route: there was no Real chain to
write to and no rule deciding what belonged on it. Both now exist (UI-023). An install that never
goes online never writes a Real transaction, and one that does gets it written or queued.

**Original finding.**

**Severity: high · Status: was open**

Every one of the 2,713 non-genesis transactions carries:

```
metadata.storage_policy   = "local-blockchain-encrypted-document"
metadata.encryption       = "AES-256-GCM"
```

There is exactly one storage policy in the entire chain. Whatever the connection plumbing
supports, in practice this chain is 100% local: 1,989 `html-nn` documents, 694 `thought-state`
commits, 15 `behavioral-learning`, 15 `non-critical-event`.

> **Correction (same session).** An earlier version of this finding claimed the Real Chain "is not
> built at all." That was wrong. Remote-chain plumbing does exist —
> `_derive_chain_endpoint` ([server.py:4820](../python/harness/server.py#L4820)) normalises
> `ws://` / `wss://` / `http://` / `https://` endpoints and accepts an explicit override, and
> `_register_peer_on_chain_directory` POSTs to `{endpoint}/chain/peers/register`. Both are live,
> used by `chain.connect_vps` and `chain.auto_connect`. The accurate statement is that the
> capability exists and has never been exercised on this chain.

### UI-027 — `python/ledger/` is a dead second chain implementation
**Severity: medium · Status: fixed (2026-08-28)**

`python/ledger/` is deleted — four modules, no callers, a strict subset of the Node server's
endpoints, and pointed at the same port. Q-14 had already resolved this as safe.

Two loose ends went with it: a commented-out block in `server.py` that used to start it as a second
chain server on 8332, and `test_ledger_chain_features` in `tests/test_trust.py`. The other test in
that file covers `TrustRegistry`, which *is* live (`server.py` applies it to contacts), so it stays —
worth checking rather than assuming, since deleting a test alongside its subject is right and
deleting one that happens to sit next to it is not.

**Original finding.**


There are two complete chain servers in this repo, both targeting port 8332:

| | `blockchain/src/app.js` | `python/ledger/` |
|---|---|---|
| language | Node.js | Python (`chain.py`, `crypto.py`, `server.py`, `verify.py`) |
| status | **live** — what the UI talks to | **dead** |
| endpoints | `/chain/status`, `/chain/blocks`, … | `/chain/tx`, `/chain/import` |

`python/ledger/` is imported only by its own modules and by `tests/test_trust.py`. Its sole
reference in the harness is commented out at
[server.py:11662](../python/harness/server.py#L11662), and a comment at
[server.py:11590](../python/harness/server.py#L11590) records why:

```
# (Disabled: Tala Local Chain is now run via Node.js in blockchain/src/app.js on port 8332)
```

It still has a passing test, so it will not show up as rot. Worth an explicit decision — delete,
or keep as the Real Chain implementation — before anyone builds on the wrong one. Note its default
transaction type is `html_nn_anchor`, which is closer to a Real-Chain anchoring model than the
live Node chain's `node-create`.

### UI-025 — The renderer hardcodes the local node address
**Severity: medium · Status: fixed (2026-08-28)**

`BlockchainViewer.load()` asks `chain.summary` for `ghost_endpoint`, which the harness resolves from
the same `TALA_CHAIN_BIND_HOST` / `TALA_CHAIN_BIND_PORT` the node itself reads. The literal survives
only as a fallback when the harness cannot be reached, so a moved port shows the chain rather than an
offline message. The panel also now states which chain it is showing — Ghost, local only — which is
worth saying on a screen that has a Real chain a scroll away.

**Original finding.**


`BlockchainViewer.load()` fetches two fixed URLs
([renderer.js:6439](../renderer/renderer.js#L6439)):

```js
fetch('http://127.0.0.1:8332/chain/status')
fetch('http://127.0.0.1:8332/chain/blocks')
```

The address also appears hardcoded as a `<code>` element in the status card
([index.html](../renderer/index.html)) — it is static text, never updated from the fetch, so the
`RPC Endpoint:` readout cannot ever show anything else.

This matters for the Ghost/Real split specifically: the **node server is already remotely
bindable** — `blockchain/src/app.js:23` reads `TALA_CHAIN_BIND_PORT` / `TALA_CHAIN_PORT` from the
environment, and its header comment says it "can be bound to" a non-local address. The server can
go remote; the viewer cannot follow it.

Related: the Electron CSP at [main.js:278](../electron/main.js#L278) allows
`connect-src 'self' http://127.0.0.1:8332 https://talacore.app`. The presence of `talacore.app`
suggests a remote chain host was anticipated. Any remote endpoint outside those two would be
blocked by CSP — worth knowing before designing the Real Chain view.

### UI-026 — Transaction id truncation destroys the only ghost marker that exists
**Severity: low · Status: fixed (2026-08-28)**

The card shows the full transaction id, with the value also in the `title` attribute, and the label
is `Type` / `Creator` / `State Hash` rather than the inaccurate `UUID`. Transaction type is now shown
too, which is what actually distinguishes a `node-create` from a `payment` or a `key-revocation`.

**And the values are escaped.** They were interpolated straight into `innerHTML`, which was survivable
while every transaction was written by this machine and is not now: reconcile pulls transactions
written by other people, and `creator` is whatever they put in it.

**Original finding.**


The block card renders `tx.id.slice(0, 8)` as `UUID:`. Consequences:

- `settings-workflows-...` → `settings` (what the reference screenshot shows on every card)
- `ghost_commit_9f662455-...` → `ghost_co`

So the single existing signal of ghost-ness is clipped to a meaningless fragment, and unrelated
transactions collapse to the same displayed string. The label `UUID:` is also inaccurate — these
ids are slugs, not UUIDs, except for the genesis mint.

---

## A5. Confirmed defects — Agent Builder

**Steven's label:** *"Agent builder."* Four sub-tabs: `Workers`, `Topologies`, `Task Board`, `Runs`
(`data-builder-tab` on each; panes are `#builder-tab-<id>`).

Structurally this is the most complete screen audited. The topology editor, DAG renderer, kanban,
and swarm monitor are all real. The findings are about what leaks in and what silently drops out.

### UI-028 — The test suite writes into the live Task Board
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** PARTIAL — the 13 fixture tasks were purged and 8 junk nodes pruned, but the cause is untouched: tests still write to the live database, so fixtures return on the next pytest run.

Every task visible on the Task Board in the reference screenshot originates from the test suite,
not from real work:

| Card on the live board | Source |
|---|---|
| `Write buggy code to scratch/buggy.py` | [tests/test_graph_engine.py:269](../tests/test_graph_engine.py#L269) |
| `Write test to scratch/test_integrate.py` | [tests/test_fleet_engineering.py:90](../tests/test_fleet_engineering.py#L90) |
| `[VERIFIER] Gate that always fails` | [tests/test_swarm_orchestration_e2e.py:140](../tests/test_swarm_orchestration_e2e.py#L140) |
| `Swarm Goal: E2E blocked swarm goal` | [tests/test_swarm_orchestration_e2e.py:157](../tests/test_swarm_orchestration_e2e.py#L157) |

The card ids confirm it — `retry`, `success`, `integrate` are fixture names, and the same strings
appear verbatim in `nodes/learning-i-haven-t-been-given-a-target-yet-...html`, meaning the
pollution has already propagated from the task table into the node wiki.

**Running `pytest` mutates the user's real database.** This is the same isolation gap already
documented for the SelfDocumenter learning-node path — pinning a temp directory is not sufficient
on its own, because these write paths resolve their own data root. The board cannot be trusted as
a view of real work until tests are isolated, and the existing fixtures need purging.

This also makes the whole screen hard to audit: there is no way to tell, from the UI, which tasks
are genuine.

### UI-029 — Task descriptions are injected into `innerHTML` unescaped
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** Description, worker id, task id and branch name all pass through
> `escapeHtml`. The null guard went in at the same time: `t.description.length` on a null
> threw inside `forEach`, the outer catch swallowed it, and the whole board rendered empty
> with only a console error to show for it.

The kanban card is built by string interpolation with no escaping
([renderer.js:12588](../renderer/renderer.js#L12588)):

```js
card.innerHTML = `
  ...
  <div style="font-size: 0.85rem; line-height: 1.3;">${shortDesc}</div>
  ...
`;
```

`shortDesc` is `t.description` straight from the database. Task descriptions are written by the
goal engine and by model output, and goals can be seeded from web research — so untrusted text
reaches `innerHTML` directly. Compare the Research view, which routes every equivalent field
through `escapeHtml()`; that helper exists and is simply not used here.

Second defect on the same line: `t.description.length` is read without a null guard. One task row
with a null description throws inside the `forEach`, the outer `try/catch` swallows it to
`console.error('Failed to load tasks')`, and **the entire board renders empty** with no message to
the user.

### UI-030 — The Task Board silently shows only the newest 50 tasks
**Severity: medium · Status: open**

`task.list` is hardcoded ([server.py:9020](../python/harness/server.py#L9020)):

```sql
SELECT ... FROM tasks ORDER BY created_at DESC LIMIT 50
```

There is no pagination, no total count, and no indication in the UI that anything was omitted. A
board showing seven columns of work is in fact showing at most 50 rows — silent truncation reading
as complete coverage.

### UI-031 — Swarm-root detection depends on a single-level payload decode
**Severity: medium · Status: open — needs verification against live data**

Swarm monitoring is gated on ([renderer.js:12626](../renderer/renderer.js#L12626)):

```js
if (t.payload && t.payload.topology_id && !t.parent_id) { ... }
```

`task.list` decodes defensively but only **one** level deep
([server.py:9032](../python/harness/server.py#L9032)):

```python
"payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
```

This codebase has a documented double-encoding problem on JSONB columns — a jsonb codec plus
`json.dumps` at the call site leaves the column holding a JSON *string*. For any such row a single
`json.loads` yields a string, not a dict, `t.payload.topology_id` is `undefined`, and the task
silently loses its blue border, its click handler, and its ability to be monitored in the Runs tab.

Not asserted as currently broken: the reference screenshot **does** show one correctly-detected
swarm root (`Swarm Goal: E2E blocked swarm goal`), so at least the swarm-spawn writer encodes
correctly. The risk is per-writer and needs a check across all task-creating paths.

### UI-032 — `Assign` asks the user to type a worker id into a browser prompt
**Severity: low · Status: open**

([renderer.js:12604](../renderer/renderer.js#L12604))

```js
const workerId = prompt('Enter Worker ID to assign (e.g. from Workers tab):', ...);
```

A raw `prompt()` requiring the user to remember and retype an id — while the Workers tab, one
click away, already holds the authoritative list (`tala-rp`). No validation on the entered value
either; a typo assigns a task to a worker that does not exist.

### UI-033 — `Manage Workers in Settings` targets a selector that does not exist
**Severity: medium · Status: open**

([index.html](../renderer/index.html), Workers pane)

```html
onclick="document.querySelector('[data-view=\'settings\']').click();
         setTimeout(()=>document.querySelector('[data-tab=\'settings-agents\']').click(), 100)"
```

> **Correction (same session).** First recorded as a *timing race* on the 100 ms `setTimeout`.
> Verified against the markup: it is worse than that. There are **zero** elements with
> `data-tab="settings-agents"` anywhere in `index.html`. Settings panels are selected by
> `data-panel`, and the agent panel is `data-panel="agent"`. The second `querySelector` returns
> `null` on every run regardless of timing, and `.click()` on null throws.

So the button always drops the user on Settings showing whichever panel was last active — never
the Agent panel — and raises an uncaught `TypeError` in the console. The `setTimeout` is
irrelevant; no delay would help.

Directly relevant to UI-007: this is the only cross-view navigation in the codebase, it is done by
synthesising DOM clicks against a hardcoded selector, and that selector is stale. Any `ui.navigate`
tool built for Tala should replace this mechanism, not copy it.

---

## A6. Confirmed defects — Mission Control

**Steven's label:** *"Mission control. These are tasks being worked on. In this section we have a
problem that they don't fade out. It stays forever. Even if I manually clear it, a few runs later
they come back."*

Steven's report is **two separate bugs** with two separate causes. Both are diagnosed below.

### UI-034 — Nothing ever ages a task out ("it stays forever")
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** `prune_finished_tasks` ages terminal-status tasks out after 14 days, run at
> startup and exposed as `task.prune` with a dry run. Only finished tasks are ever eligible:
> an abandoned in-progress row is a bug worth seeing, and deleting it hides the bug rather
> than the row. Separate from `task.clear`, which truncates everything including running
> work. Every sweep writes what it removed to `agent_log` -- a retention policy that deletes
> without a record is indistinguishable from data loss.

There is **no retention policy for tasks anywhere in the codebase.** `task.list` is a flat
newest-first window with no status filter and no age filter
([server.py:9020](../python/harness/server.py#L9020)):

```sql
SELECT ... FROM tasks ORDER BY created_at DESC LIMIT 50
```

A search for every `DELETE FROM tasks` in the repo returns exactly two production call sites:

| Path | Scope | Trigger |
|---|---|---|
| `task.delete` ([server.py:9492](../python/harness/server.py#L9492)) | one row + its children | user clicks `Delete` |
| `task.clear` ([server.py:9445](../python/harness/server.py#L9445)) | `TRUNCATE TABLE tasks CASCADE` | user clicks `Clear Queue` |

Both are user-initiated. Nothing automatic, scheduled, or status-driven ever removes a row. A
task that reaches `complete` or `blocked` stays in the queue permanently and keeps rendering as a
card, indistinguishable in prominence from live work.

That is the "doesn't fade out" half, and it is not a bug in the sense of broken code — the
behaviour was simply never written. Completed work has no exit path from the queue.

Compounding it: the 50-row `LIMIT` (UI-030) means that once 50 stale completed tasks accumulate,
**new tasks stop appearing on the board at all**, with no message explaining why.

### UI-035 — Cleared tasks reappear because the test suite re-inserts them
**Severity: high · Status: FIXED 2026-08-23 · verified, was already fixed · Root cause of Steven's "they come back"**

> **Already fixed, and now proven.** I proposed this batch believing the root cause was still
> live. It is not: `conftest.py` redirects the suite to a separate database before any import
> can read `settings.DB_URL`. Verified two ways -- the suite resolves `tala_test` while the
> app resolves `tala`, and a full run left the live task table unchanged at 3 rows before and
> after. A test now pins it, so it cannot regress silently.

`task.clear` genuinely works — `TRUNCATE TABLE tasks CASCADE` wipes the table, and the UI confirms
with an accurate count before firing. So reappearance cannot be a failed delete. Something is
**re-creating** the rows.

It is the test suite, and the mechanism is precise:

1. Tests write to the **live database** (already recorded as UI-028), not an isolated one.
2. Tests use **fixed, hardcoded task ids**, not random ones.
3. Cleanup is a **pre-test reset, not a post-test teardown**. From
   [tests/test_graph_engine.py:15](../tests/test_graph_engine.py#L15):

```python
async def _reset_task(task_id: str) -> None:
    """Drop any leftover row (and cascaded graph_nodes/events) from a prior
    run of this same fixed task_id, so DAG execution can't short-circuit on
    stale 'already completed' state."""
    await db.execute("DELETE FROM tasks WHERE id = $1", task_id)
```

The docstring is explicit that rows survive between runs — it exists to clean up *the previous
run's* leftovers. The row is deliberately left in the database when the test finishes.
`tests/test_swarm_orchestration_e2e.py` has no delete at all.

So the loop Steven observes is exactly:

> clear the queue → run the test suite → the same fixtures reappear, with the same ids and the
> same descriptions → clear again → they come back again

"A few runs later" is a few `pytest` runs. This is not a Mission Control bug at all — the view is
faithfully displaying rows that the test suite keeps putting back.

**Note the two findings interact.** Fixing UI-035 alone stops new pollution but leaves the
existing stale rows forever (UI-034). Fixing UI-034 alone ages out fixtures that the next test run
immediately recreates. Neither is sufficient on its own.

---

## A7. Confirmed defects — Vault

**Steven's label:** *"Vault section does not work at all. It is meant for storage of editable
private documents. Things the local LLM could reference, but nothing gets shared outside."*

Confirmed: it does not work. But the split matters — **the security backend is real and well
built; everything above it is missing.** This is not an abandoned idea, it is a foundation with no
building on it.

What exists and is genuinely good:

- `vault:read` IPC ([ipc-handlers.js:384](../electron/ipc-handlers.js#L384)) — path-traversal safe
  via `safePath(vaultDir, filePath)`, size-capped at `MAX_READ_BYTES`, rejects non-regular files.
- **The isolation guard works.** Reads are refused outright unless `isNetworkIsolated()` is true:
  `"vault:read blocked: network connections are active."` That is precisely Steven's
  "nothing gets shared outside" requirement, already enforced at the IPC boundary.
- `vault.status` RPC ([server.py:11020](../python/harness/server.py#L11020)) returning path,
  existence, file count and total size.
- A real `vault/` directory with `.keys/`, `character/`, and wallet key material.

### UI-036 — The Vault view is a static placeholder with no JavaScript
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** The view is built: a document list with a filter, a title/tags/body editor, and
> the per-document access controls. `VaultManager` is registered in the init sequence and
> loads when the view is opened -- a module nothing tells to load is the same as no module.
>
> Two habits the panel keeps, because this is the one screen where getting them wrong is a
> disclosure rather than an annoyance. Grants are rendered from what is **stored**, not from
> what was last typed, so a grant that failed to persist cannot look applied -- which meant
> `vault.list` had to start returning them, or the controls would have rendered "off"
> whatever they were set to. And the document write and the grant write are separate calls
> and separate failures: a save that loses its permissions says so rather than reporting
> success.
>
> Still a text editor only: there is no file drop or import, so a PDF or an image cannot go
> into the vault yet. Recorded on `ui-view-vault` so the map does not overstate it.

> **Backend complete, UI not started.** Everything the view needs now exists: `vault.list`,
> `vault.read`, `vault.write`, `vault.delete` and `vault.grant`, with documents sealed at
> rest and per-document grants. The eight lines of placeholder HTML in `index.html` are
> unchanged and there is still no JS module, so this stays open until the view is built.

The entire view is eight lines of HTML:

```html
<section class="view" id="view-vault" aria-label="Vault" hidden>
  <div class="view-header">
    <h1 class="view-title">Vault</h1>
    <span class="badge badge-secure">Network Isolated</span>
  </div>
  <div class="empty-state">
    <p>Vault is empty. Drop files here to store them securely.</p>
  </div>
</section>
```

There is no file list container, no drop zone element, no file input, and **no JS module** — the
only `vault` references in `renderer.js` are the view-id mapping and the tab title. Every other
view has a corresponding module (`WebBrowser`, `ResearchManager`, `BlockchainViewer`,
`MissionControl`, `AgentBuilder`); the Vault has none.

**"Drop files here to store them securely" is not true.** No `drop` or `dragover` handler is bound
anywhere. Dropping a file on this view does nothing — and because Electron's default is to
navigate to a dropped file, the instruction invites an action the app has not handled.

Likewise "Vault is empty" is hardcoded. It would say that with a thousand files in the directory.

### UI-037 — There is no write path, so "editable documents" is impossible
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** `vault.write`, `vault.read`, `vault.list`, `vault.delete` and `vault.grant`.
> A vault document is an HTML-NN typed `vault` -- Steven's framework, *"we started that
> framework with the html-nn being labeld as vault"* -- so it is not a second kind of
> storage and inherits the parser, the schema and the portable data root. The body is sealed
> before it touches disk; the title is not, because a vault whose contents cannot be listed
> without the key is one nobody can use.

`vault:read` is the **only** vault channel exposed in
[preload.js:28](../electron/preload.js#L28). There is no `vault:write`, no `vault:list`, no
`vault:delete`, no rename.

Consequences for the stated purpose:

| Stated intent | Status |
|---|---|
| storage of documents | no way to put a file in through the app |
| **editable** documents | no write channel at all |
| listing what is stored | `vault.status` returns a *count*, never filenames — even a finished UI could not enumerate the vault |
| nothing shared outside | **works** — the isolation guard enforces it |

Only the security constraint is implemented. The storage feature it is guarding does not exist.

### UI-038 — The local LLM cannot reference vault documents
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** `vault_list`, `vault_read` and `vault_search`. Read-only: putting documents in
> and taking them out stays with the user, since a private store an agent can silently
> rewrite is not a private store. A refusal is returned as a refusal rather than as an empty
> document, and search withholds per document so one closed file does not hide the rest.
>
> Vault bodies **are** indexed locally into ChromaDB, by Steven's decision: the index holds
> plaintext and never leaves the machine, which is the trade that makes the vault findable
> rather than merely storable.

No vault tool is registered in `sdk/agent/tools.py`. Searching it for `vault` returns only three
unrelated hits — two string heuristics for tagging node content, and a `tala-vault-gate` creator
label. There is no `vault_read`, `vault_list`, or `vault_search`.

So *"things the local LLM could reference"* has no mechanism. Tala has no way to know a vault
document exists, let alone read one. Note this also means the vault is invisible to the Research
view's Unified Search, which searches web, sessions, nodes, and tasks — but not the vault.

Design question worth settling early (Q-19): the isolation guard blocks reads whenever network
connections are active. If Tala is running against a remote provider, a vault tool would be
refused by design. Whether "local LLM only" means *Ollama-only* needs deciding before the tool is
specified.

### UI-039 — The `Network Isolated` badge is hardcoded, not wired to the real flag
**Severity: medium · Status: FIXED 2026-08-23**

> **Fixed by removing the claim, not by wiring the flag.** The badge asserted "Network
> Isolated", a control that never worked and is no longer the rule -- sharing is governed at
> the exit now (UI-102). It reads "Sealed · never leaves", which is true of every vault
> document: encrypted at rest, excluded from sync and backup.

The badge is static markup with no JS behind it — it always reads `Network Isolated`, in every
state.

This is worse than the `READY` badge (UI-017) because a real runtime flag exists and is
authoritative: `isNetworkIsolated()` in [main.js:192](../electron/main.js#L192) is the exact
condition `vault:read` checks. The one badge in the app that should mirror a live security state
instead asserts that state unconditionally.

A user connected via `Connect VPS` sees `Network Isolated` while the vault guard would in fact
refuse every read.

---

## A8. Confirmed defects — Contacts Directory

**Steven's label:** *"This is the contacts. When finally online we will be able to add people to
this based on their remote wallet ID. This should be heavily integrated with the remote blockchain.
This also is part of what allows remote users to access local user skills."*

The UI is well built and the p2p transport is properly signed. The findings are about **who
decides what a peer may see** — the permission checkboxes do not control the path that actually
serves skills, and a remote peer can rewrite its own permissions.

### UI-040 — The `skills` permission does not gate the path that serves skills
**Severity: high (security) · Status: FIXED 2026-08-23**

> **Fixed.** p2p.marketplace_search now requires caller_wallet or caller_username, refuses callers that are not known contacts, and honours per-skill scope: shared global is visible to any known contact, contact-only requires the caller in shared_with, anything else is private. Withheld skills are not disclosed at all. The outbound caller identifies itself via _local_identity.

The Contacts workspace exposes nine sharing checkboxes, including `share-skills` and
`share-pii_vault`. For **profile** categories these are genuinely enforced, by a generic filter
([server.py:5320](../python/harness/server.py#L5320)):

```python
def _profile_for_permissions(profile_config, permissions):
    shared = {}
    for category, allowed in permissions.items():
        if allowed and category in profile_config:
            shared[category] = profile_config[category]
    return shared
```

But the path a remote peer actually uses to reach skills is `p2p.marketplace_search`
([server.py:10950](../python/harness/server.py#L10950)), and it consults **nothing**:

```python
async def handle_p2p_marketplace_search(params, req_id):
    query = params.get("query", "").lower()
    ...
    skills_dir = Path("skills")
    for file in skills_dir.glob("*.yaml"):
        ...
        items.append({..., "payload": data, ...})
```

The handler takes **no caller identity** — no wallet address, no username. It performs no
permission check, no firewall check, and returns `payload: data`, the **entire skill definition**,
not merely a name and description.

So any peer that can reach the p2p endpoint can enumerate and pull the full contents of every file
in `skills/`, regardless of what the local user has ticked. The checkbox implies a gate that this
path does not have.

Note this is precisely the capability Steven named — *"allows remote users to access local user
skills"* — so the feature works. It is the consent mechanism around it that is absent.

### UI-041 — An enrolling peer chooses its own permissions
**Severity: high (security) · Status: FIXED 2026-08-23**

> **Fixed.** Enrolment starts from deny. A peer requested_permissions block is recorded under _requested and grants nothing, and re-enrolment no longer touches the permissions column, so a grant cannot be reset by the party it governs.

On inbound enrollment ([server.py:5373](../python/harness/server.py#L5373)) the packet's
`requested_permissions` is written to the contacts row verbatim:

```python
permissions = packet.get("requested_permissions")
if not isinstance(permissions, dict):
    permissions = { "identity": True, ..., "skills": False, "nodes": False, "pii_vault": False }

await db.execute("""INSERT INTO contacts (...) VALUES (...)
    ON CONFLICT (username) DO UPDATE SET ... permissions = EXCLUDED.permissions ...""")
```

Two consequences:

1. The conservative defaults (`skills`, `nodes`, `pii_vault` all `False`) apply **only** when
   `requested_permissions` is missing or not a dict. A peer that sends the field gets exactly what
   it asked for, including `pii_vault: true`.
2. `ON CONFLICT DO UPDATE` means enrollment **overwrites an existing contact's permissions**. A
   peer Steven had previously restricted can re-enroll and restore its own access.

Mitigating factor, worth stating plainly: this is not unauthenticated. The packet is HMAC-verified
against an enrollment secret first (`_verify_packet`, `hmac.compare_digest`), so only a peer
holding that secret can enroll. But the local user's checkboxes are still not authoritative — a
holder of the secret sets its own rights, and the UI gives no indication this has happened.

### UI-042 — Marketplace payloads are "verified" against a hardcoded placeholder key
**Severity: medium · Status: open**

Every item returned by `p2p.marketplace_search` carries:

```python
"public_key": "local-node-pubkey-0xTALA"
```

A literal string, identical for every skill from every peer. `handle_marketplace_download`
([server.py:10975](../python/harness/server.py#L10975)) then rejects an item that is *missing* a
`public_key` — "item missing cryptographic signature" — and logs
`"Verifying marketplace payload against public key %s..."`.

So the download path presents itself as performing signature verification while the value it
verifies against is a placeholder that carries no cryptographic meaning. A rejection for a missing
key reads as a real security control and is not one.

### UI-043 — Contacts have no blockchain integration
**Severity: medium · Status: open**

Steven's stated intent is that this be *"heavily integrated with the remote blockchain"*. Currently
the wallet address is an **opaque string in a Postgres table** and nothing more:

- `contacts.save` writes `username`, `wallet_address`, `endpoint_url`, `shared_secret`,
  `permissions` to the `contacts` table. No chain call.
- The wallet address is never validated — no checksum, no format check, no on-chain existence
  lookup. The modal's `0x71C7656EC7ab88b098defB751B7401B5f6d8976F` is placeholder text only.
- No contact is registered on, or resolved from, the chain. The peer-directory plumbing that does
  exist (`_register_peer_on_chain_directory` → `/chain/peers/register`, see UI-024) is not called
  from any contacts path.

This is consistent with the Real Chain not yet being exercised (UI-024) — the integration is
pending the same missing piece, not separately broken.

---

## A9. Confirmed defects — Settings chrome (affects **all 18 panels**)

Found while auditing `settings.inference`, but these controls sit above the panel nav and are
present on every settings tab.

### UI-044 — The environment and scope selectors are cosmetic
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** The presets now set what they claim. This one mattered more than a dead button
> usually does: `USB (ISOLATED)` reads as a security guarantee, so selecting it and having
> nothing change was worse than not offering it. The **scope** buttons are still cosmetic and
> now say so, rather than logging a line that implies otherwise.

`USB (ISOLATED)` / `LOCAL (SYSTEM)` / `REMOTE (CLOUD)` and `GLOBAL` / `WORKSPACE` are handled at
[renderer.js:691](../renderer/renderer.js#L691) by:

```js
btn.addEventListener('click', () => {
  envBtns.forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  IPCLog.append('info', `Environment: ${btn.dataset.env.toUpperCase()}`);
});
```

Class toggle plus a log line. Nothing anywhere reads `data-env` or `data-scope` — both appear only
in `index.html` and this handler. Identical pattern to UI-004/005/006.

This one is more consequential than a dead button, because **`USB (ISOLATED)` reads as a security
control.** A user selecting it would reasonably believe they had switched the system into an
isolated mode. Nothing changes. Compare UI-039: real isolation state lives in `isNetworkIsolated()`
and gates `vault:read` — this selector is not connected to it.

Equally, `GLOBAL` vs `WORKSPACE` scope implies settings can be scoped per workspace. No setting is
scoped by anything.

### UI-045 — `EXPORT`, `IMPORT`, and `APPLY CHANGES` have no handlers
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed, two different ways.** `APPLY CHANGES` was **removed** rather than wired: it was
> the primary action of the whole settings screen, had no handler, and taught the wrong
> model — that edits were staged awaiting commit, when every panel already persists on
> change. Wiring it would have made the wrong model true.
>
> `EXPORT` and `IMPORT` are implemented. Export **omits credentials deliberately**: they are
> encrypted at rest, and an export that decrypted them would be a plaintext copy of every
> secret in the app sitting in the user's downloads folder. Import applies only the
> environment — replacing provider lists would silently delete credentials the export never
> contained.

`btn-settings-export`, `btn-settings-import`, and `btn-settings-apply` appear **only** in
`index.html` ([lines 407-409](../renderer/index.html#L407)). No listener is bound to any of them
anywhere in `renderer/`.

`APPLY CHANGES` is the primary-styled action button of the entire settings screen and does nothing.

The individual panels appear to persist on change instead — toggling `FORCE` writes immediately via
`inference.save_providers`. So the button is not merely dead, it **teaches the wrong mental model**:
it implies edits are staged and awaiting commit, when they are already live. A user who edits a
panel and then closes settings without clicking `APPLY CHANGES` has still changed the system.

`EXPORT` / `IMPORT` being dead is worth noting separately against UI-047 — if export is ever
implemented, it must not write API keys in cleartext.

---

## A10. Confirmed defects — `settings.inference`

**Steven's label:** `settings.Infrence` — the whole INFERENCE panel.

Substantially real. Local provider health checking is genuinely good; the defects are in remote
providers and in what the priority list claims.

### UI-046 — A remote provider shows `ONLINE` whenever an API key string is present
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** The remote branch now meets the standard the local branch already set: it asks
> `{endpoint}/models` with the key presented, checks the configured model is in the answer,
> caches per endpoint, and fails closed. A 401 -- a key that is present and rejected, which
> is the case this finding exists for -- is now offline rather than green.

The health loop ([server.py:11588](../python/harness/server.py#L11588)) treats the two provider
types completely differently:

```python
elif p_type == "remote":
    api_key = p_config.get("apiKey", "")
    if api_key:
        new_status = "online"
    else:
        new_status = "offline"
```

**No request is made.** For `Kimi3` and `Gemini` in the reference screenshot, `ONLINE` means only
that the `apiKey` field is non-empty. An expired key, a revoked key, a typo, a wrong endpoint, a
suspended account, or no network connection all still render a green `ONLINE` dot.

Contrast the local path, which is properly done — `GET {base}/api/tags` for Ollama and
`GET {ep}/models` for OpenAI-compatible endpoints, with results cached per endpoint, **and a check
that the specific configured model is actually in the returned list**. `new_status` also defaults
to `"offline"`, so it fails closed. That is the standard the remote branch should meet.

This compounds the panel's own stated `FALLBACK CHAIN RULES`: *"Error / timeout — On connection
failure, rate-limit, or server error, transparently skip to the next available provider."* A dead
remote provider marked `ONLINE` is selected, fails, and is transparently skipped — so the user sees
neither an accurate status badge nor an error. Same silent-degradation shape as UI-018.

### UI-047 — API keys are stored in plaintext
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** `inference_providers.config` is encrypted with the same AES-256-GCM envelope
> the audit noted was already in-repo. Selective: `model`, `engine` and `endpoint` stay
> queryable, only the key is wrapped. The six copy-pasted SELECTs became one
> `load_inference_providers()` so there is a single decrypt point rather than six chances
> to miss one -- and a missed one does not fail loudly, it hands an AES envelope to an HTTP
> client as though it were an API key.

`inference_providers.config` is `JSONB NOT NULL DEFAULT '{}'`
([db.py:162](../python/harness/db.py#L162)) and holds `apiKey` as a plain string. There is no
encrypted column, no key-vault reference, and no indirection.

Notable because this codebase already has the primitives: chain documents are encrypted with
AES-256-GCM, and `vault/.keys/` exists with real key material. Provider credentials are the one
obviously secret value in the settings surface and they are the ones stored in clear.

### UI-048 — Embedding-only providers occupy visible priority slots they do not hold
**Severity: medium · Status: open**

The panel lists eight providers numbered 1-8 by priority. Two of them —
`OLLAMA - embeddinggemma:latest` (6) and `OLLAMA - nomic-embed-text:latest` (7) — are embedding
models that **cannot serve chat** and are filtered out of the generation chain at
[server.py:2378](../python/harness/server.py#L2378):

```python
if _is_embedding_model(...):
    logger.debug("Skipping embedding-only provider '%s' for generation", r["name"])
    continue
```

The filter is correct and well documented — its comment records the adversarial failure it fixed
(`'OLLAMA - embeddinggemma:latest' failed: does not support chat`). The problem is purely that the
UI does not reflect it. The displayed priority order is not the effective fallback order, and
nothing marks these two rows as excluded. A user reordering providers is reasoning about a chain
that differs from the real one.

---

## A11. Confirmed defects — `settings.agent`

**Steven's label:** *"This should be able to use the drop down to assign the type. Selecting the
agent type of crew should allow you to create a hierarchical team. With one boss and many
subordinates that can each have a different function. I think this needs to have an area to assign
the 'crew it would be a part of.'"*

The dropdown does work — selecting `Crew (Workforce Member)` reveals `WORKFORCE ROLE` and
`CREW CATEGORY`. Both other asks are blocked at the schema, and the crew concept has no behaviour
behind it at all.

The `agents` table in full ([db.py:191](../python/harness/db.py#L191)):

```sql
id, name, type, role, crew_type, is_active, config, created_at, updated_at
```

### UI-049 — There is no way to express hierarchy
**Severity: high · Status: fixed (2026-08-28) · Steven's ask #1**

`agents` gains `reports_to` (self-referencing) and `crew_id`. A grep for `parent_id`, `manager_id`,
`reports_to` and `supervisor` used to return nothing, so "one boss and many subordinates" could not
be written down.

`crews.chain_of_command()` walks up, `subordinates()` walks down, `owns()` answers whether one agent
is anywhere above another at any depth — a department head who cannot touch anything two levels down
is not a department head.

**Loops are refused rather than tolerated.** A `reports_to` cycle makes every upward walk run
forever, so work would *stop*, not misroute. `set_membership` refuses to create one — self-reporting,
two-step and longer — and the walks are bounded and cycle-aware anyway, because a row written by hand
or by an older version is not covered by that check. `find_cycles()` reports any that exist and
repairs none: which link to break is the owner's decision, and guessing would silently restructure
their organisation.

**Original finding.**


No `parent_id`, no `manager_id`, no `reports_to`, no `supervisor` — nothing in the schema expresses
a tree. A grep for all four across `db.py` returns zero.

So "one boss and many subordinates" cannot be represented. `role` is free text
(`e.g. Developer, Finance, Auditor`) and carries no structural meaning — two agents with roles
`Boss` and `Worker` are siblings as far as the system is concerned.

### UI-050 — `crew_type` is a category, not a crew
**Severity: high · Status: fixed (2026-08-28) · Steven's ask #2**

There is a `crews` table now, so a picker has something to point at. `kind` is `team` or
`department` and `parent_crew_id` nests them — one self-referencing column rather than separate
team and department tables, because "teams of 5 reporting to departments" is a tree, and a tree with
two hardcoded levels stops being one the moment somebody adds a third.

`crew_type` stays exactly as it is, as the *classification* it always was, and the form now labels
it "a classification, not a membership" beside the crew picker that carries the actual membership.

Disbanding releases members rather than deleting them, and lifts sub-crews to the disbanded crew's
own parent — disbanding a department must not delete the people in it, and `ON DELETE SET NULL`
alone would have left its teams pointing at nothing.

**Original finding.**


`CREW CATEGORY` writes to `agents.crew_type`, and its values are classifications
(`Business (Corporate, Financial, Analytics)`, etc.) — **not the identity of a specific crew**.

There is no `crews` table anywhere in the codebase, and no `crew_id` foreign key. Two agents both
set to `Business` are not thereby members of the same crew; they merely share a label. So there is
no "crew it would be a part of" to assign, because crews do not exist as entities.

Steven's instinct is right, and the gap is one level deeper than the UI: it needs a crew entity
before it can need a picker.

### UI-051 — The crew fields are write-only; nothing consumes them
**Severity: high · Status: fixed (2026-08-28)**

The whole point of this batch. `crew.assign` writes `tasks.assigned_agent_id` — the column the
executors actually read, the same one `task.assign` and the topology engine write — so putting an
agent in a crew now changes where work goes rather than a chip on a card.

| Call | What it does |
|---|---|
| `crew.assign(task, crew, role)` | routes work into a crew, preferring a member whose role matches |
| `crew.escalate(task, reason)` | sends it one step up the reporting line |
| `crews.owns(manager, agent)` | the ownership half of reporting, at any depth |

Selection is **deterministic**, which is the definition rather than an implementation choice: a
matching role first, then any active member, in a stable order. A test asserts the module contains
no `random`, `choice` or `shuffle` — agents negotiating who takes the work would make this a swarm.

Escalation moves one step per call. Going straight to the top would skip whoever is most likely to
be able to help, and looping to the top inside one call would hide where it actually got stuck. Each
escalation is written to the audit log, since work climbing the organisation is exactly what somebody
will later need explained.

Edge cases that could have been silent: an empty crew refuses the work rather than dropping it, and
a crew whose members are *all* inactive takes it and says so — refusing would lose the task, and
accepting quietly would leave nobody wondering why nothing moves.

**Original finding.**


`crew_type` and `type='crew'` are persisted and read back **for display only**:

| Site | What it does |
|---|---|
| [server.py:4549](../python/harness/server.py#L4549) | `agent.save` INSERT/UPDATE |
| [server.py:4501](../python/harness/server.py#L4501) | `agent.list` SELECT for the UI |
| [settings_nn.py:298](../python/harness/settings_nn.py#L298) | renders a table row into the settings HTML-NN |

That is the complete set. **No orchestration, routing, dispatch, or team-assembly code reads
either field.** Marking an agent as `Crew` changes nothing about how it behaves or how work reaches
it — it changes a chip on a card.

Classic dead-subsystem shape: the form is built, the columns exist, the data round-trips, and no
consumer ever looks at it.

### The hierarchy engine already exists — in Agent Builder
**Not a defect. Recorded because it changes how UI-049/050 should be fixed.**

The swarm topology system already does everything Steven describes: named nodes with distinct
functions, directed edges between them, conditional routing, and execution by `SwarmEngine`. The
`Code Repair Swarm` is exactly "many workers with different functions" —
`planner → coder → tester → reviewer → documenter`.

And the bridge is **already half-built**. `TopologyNode`
([sdk/agent/topology.py:39](../sdk/agent/topology.py#L39)) carries:

```python
#: Optional persona (an `agents.id`) this node runs as. None means the node
#: runs with no persona of its own, which is the default.
agent_id: str | None = None
```

So a topology node can already bind to a persona created in this panel. What is missing is the
reverse direction — a persona has no idea which topology it belongs to — and any notion of
seniority between nodes.

This makes the design question sharper than "add a parent column": **are crews a second
orchestration concept, or are they a friendlier front-end onto topologies?** Building crew
hierarchy from scratch would duplicate a working engine. See Q-28.

---

## A12. `settings.user profile` — chrome only

**Steven's label:** *"settings.user profile. We will go separate on these."* — the nine
sub-sections are being audited individually. This entry covers only the shared chrome above them.

### Sub-section coverage

Sub-tabs are `#profile-tabs .profile-tab-btn`, selected by `data-tab`, with panels at
`#profile-panel-<tab>` ([index.html:672](../renderer/index.html#L672)).

| # | Sub-tab (`data-tab`) | Label | Mapped |
|---|---|---|---|
| 1 | `identity` | Core Identity | **yes (2026-08-23)** |
| 2 | `about` | About | **yes (2026-08-23)** |
| 3 | `rp-persona` | Roleplay Persona | **yes (2026-08-23)** |
| 4 | `pii-vault` | PII & Vault | **yes (2026-08-23)** |
| 5 | `roles` | Roles & Work | **yes (2026-08-23)** |
| 6 | `skills` | Skills & Capabilities | **yes (2026-08-23)** |
| 7 | `projects` | Projects & Portfolio | **yes (2026-08-23)** |
| 8 | `connections` | Connections | **yes (2026-08-23)** |
| 9 | `preferences` | Preferences & Privacy | **yes (2026-08-23)** |

These nine are the same nine keys as the Contacts sharing permissions (UI-040), minus `nodes` and
plus `preferences` — so what is authored here is what gets shared to peers there. Worth keeping
that correspondence in view while auditing each one.

### UI-052 — Three indicators of connection state on one screen, three levels of truth
**Severity: medium · Status: open**

The USER PROFILE panel makes an existing inconsistency visible in a single viewport. Three
controls express roughly the same concept and disagree in reliability:

| Indicator | Location | Truthfulness |
|---|---|---|
| `Chain Connection: Local Only` | this panel | **real** — live `chain.connection_get`, handles `vps` / `local` / unset / error |
| `LOCAL (SYSTEM)` env selector | settings chrome, same screen | **cosmetic** — class toggle only (UI-044) |
| `Network Isolated` badge | Vault view | **hardcoded** — always reads isolated (UI-039) |

A user reading the top of this screen sees `LOCAL (SYSTEM)` selected and `Local Only` reported and
would reasonably conclude the selector caused the state. It did not; they are unrelated. And the
Vault asserts isolation independently of both.

Recorded as its own finding because the fix is convergence, not three separate repairs: one
authoritative source (`chain.connection_get` / `isNetworkIsolated()`) that all three read from.

---

## A13. Confirmed defects — user profile: About / Roleplay Persona / PII & Vault / Roles & Work

Four sub-sections audited together. **No real PII values are recorded in this document** — field
structure only.

All four sections save correctly into `profiles.config` under their own keys (`about`,
`rp_persona`, `pii_vault`, `roles`) and round-trip to the form. The findings are about PII
handling.

### UI-053 — "The AI agent consults these mappings" is not true
**Severity: high · Status: fixed (2026-08-28)**

The panel no longer claims the agent consults these mappings, because it does not — and the vault
work since this was filed made it *impossible*: `pii_registries` is a sealed field and
`user_profile_lookup` refuses the `pii_vault` section at every visibility setting. The claim had
gone from unimplemented to actively contradicted by design.

The replacement says what is true: the values are encrypted at rest, no agent can read them, and
they are kept for the user rather than for Tala. Asking somebody to enter paths to their personal
records on a promise the system is built to break was the worst-shaped untruth in the audit.

**Original finding.**


The PII & Vault panel states ([index.html:789](../renderer/index.html#L789)):

> *"Manage your legal credentials, sensitive contact paths, and document repository directories.
> **The AI agent consults these mappings to locate your personal records for automated task
> execution.**"*

`pii_vault.registries` — the `Secure Locations & Document Paths` list — is written in three places
and read by none:

| Site | What it does |
|---|---|
| [server.py:4002-4034](../python/harness/server.py#L4002) | reads `pii_registries` from the save params |
| [server.py:4186](../python/harness/server.py#L4186) | persists it into `config.pii_vault.registries` |
| [server.py:4067](../python/harness/server.py#L4067) | renders it as `<li>` items into a settings HTML-NN |

**No tool, agent loop, or task path reads it.** (The `capabilities.py` mentions of "registries"
refer to the *tool* registry and are unrelated.) The agent cannot consult these mappings because
nothing exposes them to it — the same shape as the Vault's "Drop files here" (UI-036) and the
sibling of UI-038, where the Vault has no tool either.

Asking a user to enter document paths to their personal records, on the stated promise that the
agent will use them, is worth correcting promptly whichever way it is resolved.

### UI-054 — PII is mirrored into a Neural Node that bypasses the private-memory guard
**Severity: high (privacy) · Status: FIXED 2026-08-23 — final hop needs confirmation**


> **Fixed, to Steven's design.** He proposed it mid-session: *"Maybe the nodes that
> containe PII can be tagged vault and anything in vault is encrypted"*. Better than the
> alternative, which was to strip PII out of the node -- that worked but made Postgres the
> real store and the node a lossy summary, against the HTML-NN mandate. `node_vault.py`
> seals the vault fields in the node itself, so the node stays the record. `parse_html`
> deliberately does **not** auto-unseal, or the encryption would be decoration: every
> existing reader, retrieval included, would carry the values straight back out. The prose
> retrieval indexes now carries a summary saying the vault exists without saying what is in
> it. Both node-writing paths seal -- an earlier pass reached only one, which a test caught.

The `memory_index` path handles PII carefully (see D11 below): the chunk is tagged
`trust_label='private'` and filtered out of search unless `allow_private=true`.

But the profile is **also** written to an HTML-NN. [server.py:6255](../python/harness/server.py#L6255)
reads `pii_vault` into local variables and, per the comment at
[server.py:6291](../python/harness/server.py#L6291), writes the **full config** into the node:

```
# state_data carries the FULL config (identity/about/roles/
# rp_persona/pii_vault/skills/projects/connections/
# preferences/privacy) as the single source of truth for
# this node -- per the HTML-NN universal data mandate ...
```

`nodes/concept-user-profile.html` confirms it — the file carries `legal_name`, `email`, and
`phone` keys, and:

```
data-trust-tier="system"
```

**Not `internal`.** And `p2p.semantic_search`
([server.py:10705](../python/harness/server.py#L10705)) filters with exactly one predicate:

```python
where_filter = {"trust_tier": {"$ne": "internal"}}
```

So `system` passes. The careful `trust_label='private'` guard protects one copy of the data while a
second copy sits in the node corpus behind a filter it does not trip.

**Stated as needing confirmation, not asserted:** this depends on `concept-user-profile.html`
actually being present in the vector index that `retrieve_relevant_nodes` queries. That last hop
was not verified. The mechanism and the trust-tier mismatch are confirmed; whether a remote peer
can retrieve it today is not.

Note this interacts with UI-040/041 — a peer can already set its own permissions on enrollment, and
`pii_vault` is one of the nine sharing keys.

### UI-055 — PII is stored in plaintext
**Severity: medium · Status: FIXED 2026-08-23**


> **Fixed.** `profiles.config["pii_vault"]` is encrypted section-wide rather than by
> field name: every value in the vault is sensitive by definition, and matching names would
> mean a field added later stays in clear because nobody updated a list. All nine profile
> readers decrypt -- including the two astrology paths, which read `pii_vault.dob` and would
> otherwise have quietly stopped finding a birthdate.

`profiles.config` is `JSONB NOT NULL DEFAULT '{}'::jsonb`
([db.py](../python/harness/db.py)) with no encryption. Legal name, date of birth, email, phone, and
document paths are stored in clear, alongside everything else.

Same finding as UI-047 for API keys, and the same observation applies: this codebase already
encrypts chain documents with AES-256-GCM and maintains `vault/.keys/`. The two most sensitive
stores in the app — provider credentials and personal identity — are the two kept in cleartext.

---

## A14. Confirmed defects — user profile: the data-loss bug

**Steven's report:** *"The big problem is the data is not staying or being used. I can fill in the
information and the agent will not use it, but after a few restarts it goes away."*

Both halves confirmed. They have **different causes** and need separate fixes.

### UI-056 — Profile data can vanish because the source of truth is a file whose location moves
**Severity: critical · Status: FIXED 2026-08-23 · Steven's "it goes away"**

> **Fixed.** The node-write failure is no longer swallowed: profiles.save now returns node_synced plus a warning, and writes an ERROR to agent_log. The loader also falls back to profiles.config when the node is legacy-flat, so a stale node can no longer hide the data.

The profile's canonical store is **not the database**. It is a file:
`<user-data-dir>/nodes/concept-user-profile.html`. The load path says so explicitly
([renderer.js:11265](../renderer/renderer.js#L11265)):

> *"Always loads from the HTML Neural Node (concept-user-profile.html) via
> identity.get_user_profile ... the node is the canonical, single source of truth for the profile,
> not the profiles table (Postgres is still written on every save, for search indexing, but is not
> consulted here)."*

That directory is resolved at runtime by `resolve_user_data_dir()`
([documentation_nodes.py:109](../sdk/agent/documentation_nodes.py#L109)):

```python
user_data = os.environ.get("TALA_USER_DATA")
if user_data:
    return Path(user_data).expanduser().resolve()
return _application_root()
```

**So the profile lives wherever that resolves at the moment of the write — and it is read back from
wherever it resolves at the moment of the next read.** If those differ, the profile is not
corrupted or deleted; it is simply somewhere else, and the panel renders empty. That matches
"after a few restarts it goes away" far better than any delete path would.

Two known ways the two can diverge on this machine, both confirmed to exist:

1. **`TALA_USER_DATA` is the documented test-isolation mechanism.** The docstring names test
   isolation as its first purpose. Any process or shell that has it set writes to a different root
   than a plain launch. Compare UI-035, where the test suite is already confirmed to be writing
   into live state.
2. **There are two application roots present.** `_application_root()` resolves to the checkout for
   a dev run, but a packaged build exists at
   `dist/win-unpacked/resources/app/sdk/agent/documentation_nodes.py`. Launching the packaged app
   and the dev build alternately gives each its own `nodes/` and therefore its own profile.

Compounding it — **the database copy could not restore the profile even if consulted.** The DB
sync at [server.py:4151-4200](../python/harness/server.py#L4151) writes only:

`identity.name`, `identity.birthdate`, `preferences.tone`, `about.bio`,
`rp_persona.{name,class,faction,backstory}`, `pii_vault.{legal_name,dob,email,phone,registries}`

**Not written:** everything in Roles & Work; all of Skills, Projects, Connections; the privacy
block; and most of Core Identity (headline, location, avatar, banner, username, timezone, language,
pronouns, nickname, motto) and About (personal story, current status, goals, values).

So the file is the only complete copy. Lose track of the file and those fields are gone
permanently.

**Note this was diagnosed once already.** The same comment records a previous incarnation — two
racing load sources where the fallback "could never carry headline/location/timezone/roles/skills/
projects/connections/preferences/privacy at all". That race was fixed by making the node
authoritative. The remaining exposure is the node's *location*, not the load order.

### UI-057 — The user profile is never injected into the chat system prompt
**Severity: high · Status: FIXED 2026-08-23 · Steven's "the agent will not use it"**


> **Fixed, to Steven's design rather than the obvious one.** He rejected wholesale
> injection: *"You cant inject the profile cluster in its entirety... all other stuff
> should be looked up. The DOB is only for astrology comparison."* So the system prompt
> carries the user's name and a pointer to the `user_profile_lookup` tool; date of birth
> stays on the astrology path, which reads the database directly and is untouched.

The system-prompt assembly ([server.py:1300-1400](../python/harness/server.py#L1300)) builds from
the **active agent** record only — the persona's `system_prompt`, plus its background HTML-NN
appended as `[Personality Background / History Context]`, plus astrology style modulation.

It never reads `profiles` / `master-user`. The only reads of `master-user` anywhere are:

| Site | Purpose |
|---|---|
| [server.py:3723](../python/harness/server.py#L3723), [3801](../python/harness/server.py#L3801) | astrology transits (DOB only) |
| [server.py:5311](../python/harness/server.py#L5311) | building the payload shared to **peers** |
| [server.py:4153](../python/harness/server.py#L4153), [6250](../python/harness/server.py#L6250) | the save path itself |

None of these is the chat path. So the agent genuinely cannot use the profile: name, bio, role,
skills, projects, preferences and tone never reach it. The only indirect route is memory retrieval
(`index_profile_chunks` does index the profile), and PII chunks are excluded from that by default.

Worth noting the asymmetry: **the profile is wired to be shared with peers, and not wired to be
used by your own agent.**

### UI-058 — The Privacy & Visibility dropdowns are not enforced
**Severity: high (privacy) · Status: FIXED 2026-08-23**


> **Fixed.** `config.privacy` is read for the first time. Each lookup is gated by that
> section's visibility: `public` is readable by the local agent, `connections` is not
> (Tala is not a connection — Steven's invite-only model), and a section with no setting
> is withheld, so adding a profile section cannot silently publish it. Doing this as a
> per-section lookup rather than retrieval is what gave the setting somewhere to be
> enforced at all.
>
> **Migrated 2026-08-28.** The visibility setting is now the *fallback*, not the only rule:
> an explicit grant to a named agent opens one section without changing the global setting
> for everyone, and lockdown overrides both. A grant to one agent does not open it to
> another — an agent is a principal, not a category.

Preferences & Privacy offers six visibility selectors — Core Identity, About Biography, Work &
Roles, Skills, Projects, Connections Graph — under the heading *"Configure access control
permissions for other peers and agents."* All six default to **Public**.

`config.privacy` is written as an empty dict ([server.py:3961](../python/harness/server.py#L3961))
and **read by nothing**. A grep for `privacy` in the harness returns only that write, three
unrelated `data-privacy="private"` HTML attributes, and a comment. No access-control path consults
it.

The actual gate on peer access is the Contacts permission set (UI-040) — a different surface, with
different keys, that this panel does not feed. So a user who sets these to Private gets no
protection and has been told they configured access control.

---

## A15. Confirmed defects — `settings.workflows`

**Steven's label:** *"This is the workflow section. Two things here. One it doesn't seem to connect
to anything, and 2 Every tool created in the html-nn should be available here. In our system
workflows are based on html-nn that have been chained together."*

Both confirmed. The visual builder itself is real — drag-drop canvas, SVG edges, properties panel,
JSON editor, import/export. The failures are at both ends: what can go *into* a workflow, and
whether anything can *run* one.

### UI-059 — The palette is five hardcoded types; none of the 66 tools are available
**Severity: high · Status: FIXED 2026-08-23 · Steven's point 2**

> **Fixed.** The palette now has a tool section fed by `tools.catalogue`, which reads the
> `tool-*.html` nodes rather than the Python registry — a palette built from the registry
> would leave the nodes decorative, which is what UI-107 just fixed. All 90 tools appear,
> with a filter because ninety items need one. Dropping one creates a `tool` node carrying
> `config.tool` and `config.args`, pre-filled from the schema's required arguments, and the
> properties panel edits those arguments as JSON. Unparseable JSON is **not saved** — it
> shows an error and keeps the last good value, because silently writing `{}` would strip
> the user's work and run the step with no arguments at all.
>
> The five original types stay: `trigger` and `output` are structural and have no tool
> equivalent.

> **Half fixed.** Steven's requirement was *"Every tool created in the html-nn should be
> available here."* The executor now has a `tool` node type that reaches all 90 registered
> tools through `ToolRegistry.run_tool`, so a workflow *can* call any of them, and each
> tool now has a `tool-*.html` node carrying its argument schema (UI-107) — which is what
> a palette would be built from. The palette markup in `index.html` is still five static
> `<div data-type>` elements; that part remains open.

`#workflow-palette` is five static `<div data-type="...">` elements in `index.html`:

| `data-type` | Label |
|---|---|
| `trigger` | Trigger Node — workflow entry point |
| `auth` | Authentication Node — store provider secrets |
| `email` | Email Node — send SMTP email alerts |
| `custom_script` | Custom Script — run Python instructions |
| `output` | Output Node — workflow results gatherer |

Nothing populates it at runtime. `renderer.js` only queries `.palette-item` to attach drag handlers
([renderer.js:9258](../renderer/renderer.js#L9258)) — it never adds items.

Meanwhile `sdk/agent/tools.py` carries **66 registered tools**, and the repo holds 128 HTML-NN
nodes. None are reachable from this builder. So a workflow can trigger, authenticate, send an
email, run a raw Python string, or collect output — and nothing else. `custom_script` is the only
escape hatch, and it bypasses the tool registry rather than using it.

This is the gap Steven describes: workflows are meant to be HTML-NN chained together, and the
builder cannot reference a single HTML-NN.

### UI-060 — The agent cannot run a saved workflow, though the node tells it to
**Severity: high · Status: FIXED 2026-08-23 · Steven's point 1**


> **Fixed.** `execute_node` is a registered tool, so the instruction the compiled node
> carries is one Tala can now follow. The node is also what runs: `workflow.execute` read
> a JSON sidecar, which left the compiled HTML-NN written, indexed and never executed.
> Both paths now load the spec from the node's own `node-state` block, with the sidecar
> kept only as a fallback for flows compiled before this change.

The panel's own description states:

> *"Saved workflows are compiled into HTML Neural Nodes in your cognitive graph, **allowing the
> agent to activate and call them from memory**."*

The first half is true — `workflow.save` compiles a node via
[workflow_compiler.py](../sdk/agent/workflow_compiler.py) and writes it to
`resolve_active_nodes_dir()`.

The second half is not. The compiled node contains this instruction
([workflow_compiler.py:69](../sdk/agent/workflow_compiler.py#L69)):

> *"This workflow can be executed by invoking the RPC method `workflow.execute` with the Flow ID
> `flow-{flow_id}`."*

But `workflow.execute` is a **JSON-RPC method, not a registered tool**. The agent can only call
tools in the registry, and of the 66 registered the only workflow-related one is
`workflow.generate_from_tot` — which *creates* a workflow and cannot run one. There is no
`workflow.list` tool, no `workflow.execute` tool, and no generic "invoke an RPC method" tool.

So the chain breaks at the last link: the workflow is built, compiled into a node, and the node
correctly documents how to run it — to a reader who has no mechanism to do so. Retrieval will
surface the node and the agent will be unable to act on it.

That is "it doesn't seem to connect to anything", precisely located.

**Side observation, corroborating UI-056.** The running app shows a saved workflow
(`settings-summary-workflow`), but no `flow-*.html` node exists in the repo's `nodes/`. Consistent
with the data-root divergence — the running app is writing its nodes somewhere other than this
checkout.

---

## A16. Confirmed defects — `settings.guardrails`

**Steven's label:** *"This isn't just about what the agent can talk about. This is about the entire
harness. Guardrails should be able to address firewall rules, and port access. It should allow for
permissions from a contact and what information of yours they can access. As an example: I could
configure a user to have access to one of my agents. But that agent can only access a certain
search API, on a certain port, at a single allocated time."*

**The content guardrails are real and enforced** — a genuine exception to the dead-subsystem
pattern (see D12). The gap is one of scope, and it is architectural rather than a bug.

### UI-061 — Guardrails govern text only, not the harness
**Severity: high (design gap) · Status: FIXED 2026-08-28**

> **Fixed.** `network_guard.py` is the harness half Steven asked for: *"This isnt just about
> what the agent can talk about. This is about the entire harness."* Rules live in the same
> `guardrails_config` row, so the panel that claims to own guardrails owns all of them.
>
> **Opt-in.** With no rules configured every outbound connection is permitted exactly as
> before — the same principle as storage and databases: the option exists without being
> imposed. A firewall that switches itself on and starts denying traffic nobody asked it to
> deny is not a feature.
>
> Wired into the outbound paths rather than left as another uncalled gate: search providers
> (reported as a provider failure, not raised through the search), git remotes (local
> operations carry no URL and are unaffected), and storage (a local folder is not a network
> connection, so backing a portable install up to a second drive survives the strictest
> rules).

`GuardrailsManager` operates on exactly two things: an inbound prompt string and an outbound
response string. Every rule in the panel — toxicity threshold, PII redaction fields, SQL-injection
detection, competitor list, gibberish, blocklist — is a **text filter**.

Nothing in `guardrails_config` can express a subject (*who*), an object (*which agent or tool*), a
channel (*which port*), or a window (*when*). The table is a single row: `category` plus a `rules`
JSONB ([db.py:219](../python/harness/db.py#L219)), with a `CHECK (id = 1)` constraint — it is
global by construction and cannot hold a per-contact or per-agent policy even in principle.

### UI-062 — Access control is split across three systems that do not compose
**Severity: high (design gap) · Status: FIXED 2026-08-28**


> **Fixed at the model level.** `python/harness/access.py` is the composition point the three
> systems lacked: one vocabulary, one storage, one decision function that every enforcement
> site can call. Guardrails, contact permissions and firewall clearance are no longer the
> only options — they are three enforcement points that can now consult the same grants.
>
> Deliberately a *decision* function, not an enforcement one: enforcement belongs at each
> call site, because a function that both decides and acts is one nobody can test in
> isolation. Migrating the existing three onto it is separate work and is not claimed here.

Three independent mechanisms exist today, each with its own storage, its own vocabulary, and its
own enforcement point. None can reference another:

| System | Storage | Vocabulary | Enforced at |
|---|---|---|---|
| **Guardrails** | `guardrails_config` (single global row) | toxicity, PII fields, SQL, blocklist | prompt in / response out |
| **Contact permissions** | `contacts.permissions` JSONB | 9 booleans: identity, about, rp_persona, pii_vault, roles, skills, projects, connections, nodes | `_profile_for_permissions`; `nodes` only elsewhere |
| **Firewall** | `firewall_contacts.access_rights` JSONB | `allowed_categories`, `clearance_level`, `priority` | `_peer_can_resolve_node_reference` |

Only one path today combines two of them — node resolution requires **both** a contact permission
and a firewall record ([server.py:5084](../python/harness/server.py#L5084)). That is the one place
the model Steven describes already half-exists, and it is worth treating as the prototype rather
than starting fresh.

Steven's scenario — *user → agent → search API → port → time* — needs a policy object with a
subject, an object, and constraints. No such object exists; the three systems above cover at most
subject-and-object, and only for peer-facing data reads.

### UI-063 — No port or time-window access control exists anywhere
**Severity: high (design gap) · Status: FIXED 2026-08-28**

> **Fixed.** Ports and time windows are enforced in two places that meet: a rule may name
> ports directly, and a **grant** carries ports, hosts, an hours window and an expiry.
> Steven's example works end to end — an agent granted a search API on port 443 reaches it
> there, is refused on 8080, and is refused once the grant expires.
>
> Three deliberate orderings: **deny beats allow**, because a blocklist an allowlist can
> override is not a blocklist; **loopback stays reachable** unless explicitly denied, because
> cutting the machine off from its own local model turns a security setting into a broken app
> and that traffic never leaves the box; and a rule naming ports **does not match a
> connection whose port is unknown**, because assuming it would defeat the rule.
>
> The USB preset now blocks outbound traffic outright — it was the one preset whose name was
> a promise (UI-044) and it now keeps it.

Searches across `python/` and `sdk/` for `allowed_ports`, `port_policy`, `port_access` return
**zero** results. There is no notion of restricting anything to a port.

Time-bounded access likewise does not exist as an access-control concept. The nearest primitive is
**`sdk/agent/leases.py`**, which is genuinely well built — `expires_at`, TTL-based grants,
`is_expired()`, and `purge_expired()` ([leases.py:105-238](../sdk/agent/leases.py#L105)). But it
governs *internal resource claims* (which worker holds which resource), not who may reach what.

Recorded because it is the reusable half: the expiry/TTL machinery for "a single allocated time"
already exists and is tested. What is missing is binding it to a subject and an object.

Adjacent, also relevant: `sdk/agent/tool_selection.py` already implements a per-tier tool budget
with "visibility (logging what was withheld)" — the beginnings of restricting *which tools* an
agent may reach. It is budget-shaped rather than policy-shaped, and is not connected to contacts,
firewall, or guardrails.

**Summary of what Steven's example needs vs. what exists:**

| Requirement | Exists? |
|---|---|
| a user is granted access to one of my agents | **no** — contacts grant access to *profile data*, never to an agent |
| that agent restricted to a certain search API | **partial** — search providers are selectable per query, and `tool_selection` gates tools by budget; neither is a per-agent allowlist |
| on a certain port | **no** |
| at a single allocated time | **primitive only** — `leases.py` TTL machinery, not wired to access |

---

## A17. Confirmed defects — `settings.search`

**Steven's label:** `settings.search_provider`.

This panel is the configuration surface for the search bugs already recorded from the Research
Library audit (UI-018, UI-019, UI-020). Two findings are new to this panel, and one thing here is
genuinely better-designed than its equivalent elsewhere.

### UI-064 — The search provider `ACTIVE` badge is entirely hardcoded
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** The badge starts at `CONFIGURED`, which is a statement of fact rather than a
> health claim, and a `TEST` button runs a real query through `search.test_provider`.
> Falling through to the crawler is reported as **failed**, because that is precisely the
> failure this panel was hiding (UI-018). On demand rather than on panel load: a probe
> spends the user's own API quota, and spending it unasked is its own kind of rudeness.

The provider card renders ([renderer.js:4353](../renderer/renderer.js#L4353)):

```html
<div class="provider-status-indicator">
  <span class="status-dot status-ok"></span>
  <span class="provider-status-text">ACTIVE</span>
</div>
```

No variable, no health check, no conditional. **Every search provider always shows a green
`ACTIVE`**, whatever its state.

This is worse than the inference panel's remote-provider badge (UI-046), which at least checks that
an API key string is present. Here nothing is checked at all — a provider with a blank key, a dead
endpoint, or a revoked subscription displays exactly the same green dot as a working one.

It compounds UI-018 directly. A Tavily key that starts returning 401 causes a silent fall-through
to DuckDuckGo crawling, and this panel reports `ACTIVE` the entire time. The user has no surface
anywhere in the app that would reveal the provider is dead.

### UI-065 — The panel states failover behaviour the implementation does not deliver
**Severity: medium · Status: fixed (2026-08-27) — closed by the UI-018 work**

Already true by the time this batch reached it. The "Failover Routing" claim was only false because
a non-200 fell off the end of the branch without raising; `search_manager.query_detailed` now logs
the status, records it in `outcome.errors` with a reason, and `continue`s to the next provider. All
three stated rules hold.

**Original finding.**


`SEARCH FAILOVER & FALLBACK RULES` makes three claims:

| Stated rule | Reality |
|---|---|
| **Priority Routing** — queries check providers sequentially using the priorities defined above | **true** — `get_search_providers()` orders by `priority ASC` |
| **Failover Routing** — if a provider returns an **error** or times out, failover to the next provider immediately | **partly false** — only *exceptions* trigger the documented path. A non-200 response (401, 429, 500) falls off the end of the branch without raising, so no "failover" is logged and nothing is surfaced (UI-018) |
| **Crawling Fallback** — if no search providers are registered or all fail, direct DuckDuckGo crawling is used | **true, and this is the problem** — it is indistinguishable from success (UI-018) |

The three rules read as a coherent, well-behaved chain. The gap is that "returns an error" is
handled as *raises an exception*, and the most common real-world failures are not exceptions.

Recorded separately from UI-018 because the fix has two halves: the code must treat non-200 as
failure, **and** this panel's stated contract is what a user reasonably relies on.

---

## A18. Confirmed defects — `settings.system`

**Steven's label:** `settings.System_info`.

Most of this panel is genuinely live (see D14). One entry is actively misleading.

### UI-066 — The panel monitors a package the project never uses, and ignores the one it depends on
**Severity: medium · Status: fixed (2026-08-28)**

The panel checks `pyswisseph` — declared in `pyproject.toml`, imported by the astrology engine as
`swisseph`, and reporting `2.10.3.2` live. `ephem` is gone, along with the red alarm it raised for a
library this project neither declares nor imports.

That was the harmful half: a false alarm trains you to ignore the panel, while the dependency that
*does* matter went unwatched — and its absence does not fail loudly, it **biases** emotion labels,
collapsing roughly half of them onto one value. `cryptography` was added alongside it, since the
vault and every chain signature depend on it.

**Original finding.**


`PYTHON PACKAGES` shows **`EPHEM — not installed`** in red. That reads as a missing dependency for
the astrology subsystem. It is not.

| | |
|---|---|
| What the panel checks | `"ephem": _pkg_version("ephem")` ([server.py:7316](../python/harness/server.py#L7316)) |
| What the astrology engine imports | `import swisseph as swe` ([ephemeris.py:24](../python/astrology/ephemeris.py#L24)) |
| What `pyproject.toml` declares | `pyswisseph>=2.10.3.2` — **`ephem` is not declared anywhere** |
| Is `ephem` imported anywhere? | **No.** Zero hits across the codebase |
| Is `pyswisseph` installed? | **Yes** — `.venv/Lib/site-packages/swisseph.cp312-win_amd64.pyd` |

So the panel raises a red alarm for a library the project neither declares nor imports, while the
dependency that actually matters — and whose absence silently *biases* emotion labels rather than
failing loudly — is not monitored at all.

Both halves are wrong in the same direction: a false alarm now, and no alarm on the day
`pyswisseph` really does go missing.

> **Correction to an earlier entry in this document.** D9 (`settings.agent`) flagged the birthdate
> → astrology path with a caveat that `pyswisseph` might be absent. Verified here: it is installed
> in the active venv, and the Python version reported by this panel (3.12.9) matches the `cp312`
> build. The Chrono-Astrology style modulation is running on the real ephemeris on this machine.

### Observation — `NPM — not found`
**Severity: low · Status: open — needs confirmation**

`NODE.JS 22.15.0` resolves but `NPM` reports `not found`. npm ships with Node, so this is more
likely a resolution problem in the harness subprocess (npm is `npm.cmd` on Windows and is not
found by a bare `npm` lookup without shell resolution) than a genuinely absent npm.

Not asserted as a defect — recorded so the red text is not mistaken for a real missing dependency,
same category of noise as UI-066.

---

## A19. Confirmed defects — `settings.git`

**Steven's label:** `settings.git_provider` — *"We should be able to add any git provider
available."*

**You already can** — the form is fully generic (see D15). The problem is that adding one has no
effect on anything.

### UI-067 — Git providers are configured but never used for any git operation
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** `git.clone`, `git.repo_op` (status / pull / push / fetch / log), `git.list_repos`
> and `git.test_provider`. Providers are matched **by host**, so a GitHub token is never
> offered to a GitLab remote, and highest priority wins — which is what the panel always
> claimed.
>
> **Correction to this finding.** It recorded git as entirely unused. One thing did work: a
> `git:status` IPC — but pointed at *Tala's own checkout*, which sat against Steven's rule
> that *"Tala does not use GIT for herself."* Asked about it he said *"I may have connected
> it to check if it worked"* — leftover verification, not a design decision. It stays, now
> labelled `TALA CHECKOUT STATUS (development)` rather than `GIT WORKSPACE STATUS`, which
> implied it was the user's.
>
> **The credential handling is the part that took the care.** Three obvious approaches each
> leak it: the remote URL writes it into `.git/config` in clear and permanently; 
> `-c http.extraHeader` puts it in `argv` where any process listing shows it; a credential
> helper has to be written to disk. Git 2.31+ reads config from environment variables, which
> are in none of those places. Verified: a token used for a real fetch appears nowhere in the
> entire `.git` directory.
>
> Commands are allow-listed — a general "run git with these arguments" call is a shell — and
> absolute paths are **refused** rather than quietly relativised, because a rule that
> reinterprets its input is one nobody can reason about.

The panel states: *"Higher priority providers are used first for **clone, push, and pull**
operations."*

`git_providers` is referenced in exactly six places, all storage or display:

| Site | What it does |
|---|---|
| [db.py:262](../python/harness/db.py#L262) + migrations at 1122, 1128 | schema, `locked` and `credential_id` columns |
| [server.py:7412](../python/harness/server.py#L7412) | `git.get_providers` SELECT |
| [server.py:7451](../python/harness/server.py#L7451) | save — `DELETE` then re-`INSERT` |
| [server.py:1837](../python/harness/server.py#L1837) | a label in a settings-node section list |
| [settings_nn.py:473](../python/harness/settings_nn.py#L473) | renders rows into the settings HTML-NN |

**No git operation reads them.** Searching the harness for git commands returns two call sites,
neither provider-aware:

- `system.trigger_update` runs a bare `"git pull"` via `create_subprocess_shell`
  ([server.py:9993](../python/harness/server.py#L9993)) — no remote, no endpoint, no credential,
  no provider lookup. It operates on the harness process's own working directory.
- the `git:status` IPC handler runs `git status --short` in the workspace root
  ([ipc-handlers.js:411](../electron/ipc-handlers.js#L411)).

There is **no clone operation and no push operation anywhere in the codebase**. So of the three
operations the panel names, two do not exist and the third ignores this configuration entirely.

Priority ordering, the `FORCE` checkbox, and the stated failover rule (*"If the primary provider is
unreachable, the agent automatically falls back to the next configured provider"*) all describe
routing behaviour for a router that was never built.

Same shape as UI-051 (crew fields) and UI-059/060 (workflows): a complete, well-built configuration
surface with no consumer.

### Minor — host label inference is cosmetic and correctly falls back

`_hostLabel()` ([renderer.js:2105](../renderer/renderer.js#L2105)) infers the type chip by
substring: `github.com` → GITHUB, `gitlab.com`/`gitlab.` → GITLAB, `bitbucket.org`, `gitea.`,
`codeberg.org`, `dev.azure.com` → AZURE DEVOPS, else **`GIT`**.

Worth noting explicitly because it is the *right* pattern and the contrast with UI-019 is
instructive: this substring match only chooses a **label and a colour**, and its fallback is a
sensible generic. The search-provider substring match picks an **API dispatch branch** and its
fallback silently breaks the provider. Same technique, harmless here, damaging there.

A self-hosted host (`git.mycompany.com`) shows the generic `GIT` chip rather than being
misclassified — correct behaviour.

---

## A20. Confirmed defects — `settings.storage`

**Steven's label:** `settings.storage_provider` — *"The storage provider should be able to be
anything. And these can be used for amazon S3 storage, idrive, onedrive, sftp, google drive or any
type of storage possible."*

**The form already covers everything Steven listed** (see D16). The problem is that no storage
backend can actually be reached — there is no client for any of them.

### UI-068 — No storage client library exists for any provider type
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed, reframed by Steven:** *"A user may never install or consume any of the
> storage/git/database providers but they should have the option too."* The problem was
> never that nine types were offered — it was that none could work and nothing said so.
>
> **Three now work with no new dependency**: a local or network folder (`pathlib`), FTP/FTPS
> (`ftplib`), and WebDAV (`httpx`, already a dependency). For a portable install the folder
> target is the useful one — backing the stick up to a NAS or a second drive.
>
> The other six are declared as optional extras in `pyproject.toml` and **name the exact
> install command** when unavailable. They are deliberately *not* implemented against an
> absent library: an adapter written blind and never run is the kind of thing this audit
> keeps finding. Nothing heavy ships by default, so a user who never touches storage carries
> no extra weight.

The panel offers nine provider types. The codebase contains **no library capable of talking to any
of them**. Searching `python/` and `sdk/` for every relevant client returns zero results:

| Provider type offered | Client needed | Present? |
|---|---|---|
| S3-Compatible (AWS, MinIO, Backblaze, Spaces) — also covers iDrive e2 | `boto3` / `s3fs` | **no** |
| SFTP / SSH | `paramiko` / `pysftp` | **no** |
| Google Drive | `googleapiclient` | **no** |
| OneDrive | `msal` | **no** |
| Azure Blob | `azure-storage-blob` | **no** |
| Dropbox, FTP/FTPS, NFS, Custom | — | **no** |

None are declared in `pyproject.toml` either. This is a step beyond the git panel (UI-067), where
at least the `git` binary exists on the host — here there is no mechanism at all.

### UI-069 — Storage providers are configured but never consumed
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** `storage.backup_nodes` copies the node store to a configured provider, with a
> dry run, and `storage.test_provider` actually reaches the destination rather than showing
> a hardcoded badge. Verified end to end against a real folder target.
>
> **The vault is withheld**, and so is the Chroma index — it holds unsealed vault bodies, so
> syncing it would export the vault by the back door. The strongest check in the suite is
> that a known vault secret appears nowhere in the entire backup tree. A partly failed backup
> reports `success: false` with the per-file errors rather than a count that looks complete.

`storage_providers` appears in six places, all storage or display:

| Site | What it does |
|---|---|
| [db.py:280](../python/harness/db.py#L280) + migration 1123 | schema, `locked` column |
| [server.py:7556](../python/harness/server.py#L7556) | get providers |
| [server.py:7588](../python/harness/server.py#L7588) | save — `DELETE` then re-`INSERT` |
| [server.py:1838](../python/harness/server.py#L1838) | a label in a settings-node section list |
| [settings_nn.py:510](../python/harness/settings_nn.py#L510) | renders rows into the settings HTML-NN |

So both stated rules are false:

- *"Priority Mounting — Storage backends are mounted and traversed in priority order"* — nothing
  mounts or traverses anything
- *"Bidirectional Sync — Tala propagates changes to active storage providers in the background,
  keeping your knowledge graphs aligned"* — **there is no background sync process**

The second claim is the more consequential one: a user reading it would reasonably believe their
nodes are being backed up off-machine. Given UI-056 (profile data lost when the node directory
moves), a user might specifically rely on this as their safety net. It is not one.

### UI-070 — Storage credentials are plaintext with no indirection option
**Severity: medium · Status: FIXED 2026-08-23**


> **Fixed.** `storage_providers.credentials` is encrypted, `secret_access_key` and friends
> are redacted to the panel, and a save that sends the redaction back preserves the stored
> secret rather than overwriting a working key with three asterisks. `access_key_id`,
> `bucket` and `region` stay readable -- they identify, they do not authenticate.

`storage_providers.credentials` is `JSONB NOT NULL DEFAULT '{}'::jsonb`
([db.py:284](../python/harness/db.py#L284)) and holds `SECRET ACCESS KEY` inline.

Unlike `git_providers` and `search_providers`, this table has **no `credential_id` column at all** —
so there is not even the option of referencing a stored credential from the AUTH panel. Cloud
storage secrets are the highest-value credential in the settings surface and get the weakest
handling of the three. Same family as UI-047 and UI-055.

---

## A21. Confirmed defects — `settings.database`

**Steven's label:** `Settings.database_provider` — *"Same being able to add your own database
connections."*

The form accepts arbitrary connections (Postgres, MySQL, SQLite, MongoDB by label inference). As
with git and storage, nothing consumes them.

### UI-071 — Database providers are never used; the real connection bypasses them
**Severity: high · Status: FIXED 2026-08-28**


> **Fixed as a capability, not a policy.** Steven: *"It does not need to be used. You can
> have 0 databases or 1000 databases. How you use them is up to the user not to the
> develouper."* So `database_clients.py` does what git and storage do — connect, prove the
> connection is real, get out of the way.
>
> Postgres and SQLite need nothing extra (asyncpg is already here because Tala's own store is
> Postgres; sqlite3 is stdlib). MySQL, MongoDB, SQL Server and Oracle are declared extras
> that name their package. `database.test_provider` opens a real connection — verified
> against the live PostgreSQL 17.10 and a real SQLite file.
>
> Deliberately separate from `settings.DB_URL`: a user database going away must not be able
> to stop the app, and a test asserts the module never touches the harness pool.

`database_providers` appears in six places — schema, `locked` migration, a settings-node label, the
get, the save, and an HTML-NN table row ([settings_nn.py:546](../python/harness/settings_nn.py#L546)).
No connection code reads it.

The actual connection is a single DSN from config
([db.py:830](../python/harness/db.py#L830)):

```python
db_url = settings.DB_URL
_pool = await asyncio.wait_for(asyncpg.create_pool(dsn=db_url, min_size=2, max_size=10, ...))
```

`settings.DB_URL` is entirely independent of this table. The card showing
`Local Database (Primary) — tala@localhost:5432/tala` is a seeded display row that *mirrors* the
real connection; editing or deleting it would not change what the app connects to.

### UI-072 — "Automatic Failover" is architecturally impossible as built
**Severity: high · Status: FIXED 2026-08-28**


> **Fixed by removing the claim.** Steven: *"But remove the panel claim."* There is one pool
> from one DSN, so failover had nowhere to live — the honest fix was deleting the assertion,
> not building replication nobody asked for. `DATABASE ROUTING & REPLICATION LAWS` with
> *Priority Order* and *Automatic Failover* is gone; the panel now says the connections are
> optional and that a saved connection is not a working one until tested.

The panel states: *"Automatic Failover — If the primary replica database goes offline, operations
automatically reroute to the next healthy standby provider."*

There is **one** pool (`_pool`), created once from one DSN, guarded by
`if _pool is not None: return _pool`. There is no second pool, no standby concept, no health
monitor, and no reroute path. Failover cannot occur — not "is not implemented yet" but "the current
structure has nowhere to put it".

Same for *"Priority Order — DB connections are queried in the sequence defined"*: there is one
connection, so there is no sequence.

### UI-073 — The database provider `ONLINE` badge is hardcoded
**Severity: medium · Status: FIXED 2026-08-23**


> **Fixed.** `database.test_provider` opens a real connection and reports what happened --
> the reason matters more than the fact, since "password authentication failed" and
> "connection refused" need different responses.
>
> The same hardcoded badge turned out to be in **seven** panels, not two: git, storage,
> runtime, auth and firewall said `ACTIVE` / `MOUNTED` / `CONNECTED` / `TRUSTED` with no
> probe either. `MOUNTED` and `CONNECTED` were the worst of them, asserting a live
> connection the app never makes. All seven now render `CONFIGURED` until measured.

[renderer.js:4163](../renderer/renderer.js#L4163) renders a fixed `status-ok` dot and the literal
string `ONLINE` — no variable, no probe. Identical defect to UI-064 in the search panel.

Notable given the SYSTEM panel two tabs away performs a **genuine** PostgreSQL health check
(D14). The real signal exists in the codebase; this panel just does not use it.

### UI-074 — Database passwords are stored as bare plaintext `TEXT`
**Severity: high · Status: FIXED 2026-08-23**


> **Fixed.** A bare `TEXT` column has no config dict to walk, so the value is wrapped in
> the same envelope serialised to JSON, which still fits the column -- no schema migration
> needed. Redacted to the panel, and a redacted save keeps the stored password.

[db.py:299](../python/harness/db.py#L299):

```sql
password    TEXT        NOT NULL DEFAULT '',
```

Not JSONB, not referenced, not encrypted — a plain column. No `credential_id` either.

**Consolidated credential handling across all five provider tables**, since this is now the fifth
and the pattern is worth seeing whole:

| Table | Secret stored as | `credential_id` indirection | Finding |
|---|---|---|---|
| `search_providers` | `api_key` + AUTH join | **yes** | — (good pattern, D13) |
| `git_providers` | inline + AUTH join | **yes** | — (D15) |
| `inference_providers` | `config` JSONB, inline | no | UI-047 |
| `storage_providers` | `credentials` JSONB, inline | no | UI-070 |
| `database_providers` | `password` **plain TEXT** | no | UI-074 |

Two of five do it correctly and three do not, with no apparent reason for the split. The fix
already exists in-repo twice over.

---

## A22. Confirmed defects — `settings.runtime`

**Steven's label:** `settings.runtime_provider` — *"The intent is to be able to call a remote
collection of html-nn. This could be a friend's html_NN in which the user would need to provide a
contact endpoint/username to access."*

**That capability already exists and works** — over Contacts and p2p, identity-based and
permission-gated. This panel is a second, parallel mechanism for the same thing that has neither
identity nor permissions, and nothing consumes it.

### UI-075 — Remote instances are never queried; node retrieval is local-only
**Severity: high · Status: open**

`runtime_instances` is referenced in seven places, none of them retrieval:

| Site | What it does |
|---|---|
| [db.py:312](../python/harness/db.py#L312) + migration 1125 | schema, `locked` column |
| [server.py:7781](../python/harness/server.py#L7781) | get instances |
| [server.py:7810-7827](../python/harness/server.py#L7810) | save — `DELETE` then re-`INSERT` |
| [server.py:1840](../python/harness/server.py#L1840) | settings-node section label |
| [server.py:9366-9368](../python/harness/server.py#L9366) | **counted only** — feeds an `auth_ready` boolean |

That last one is the entire runtime use: a `COUNT(*)`, plus a count of rows with a non-empty
`api_key`, combined into a readiness flag. The rows are never dereferenced.

Node retrieval contains no remote query at all. So both stated rules are false:

- *"Top prioritized instances are queried first during node retrieval and graph propagation"* —
  retrieval never leaves the local store
- *"Fault Tolerant Failover — If a primary remote node is down, the system automatically falls back
  to secondary remote servers"* — nothing contacts a primary, so nothing can fail over

### UI-076 — This duplicates a working, identity-based capability
**Severity: high (design) · Status: open**

Steven's description — *"a friend's html_NN in which the user would need to provide a contact
endpoint/username"* — describes the **Contacts + p2p** system, which is already built end to end:

| Piece | Where |
|---|---|
| outbound node fetch from a peer | `_call_peer_rpc(endpoint, "p2p.resolve_node_reference", {"packet": packet})` — [server.py:5248](../python/harness/server.py#L5248) |
| outbound semantic search over a peer's nodes | `p2p.semantic_search` |
| inbound authorisation | `_peer_can_resolve_node_reference` — requires the contact's `nodes` permission **and** a firewall record with clearance level ([server.py:5084](../python/harness/server.py#L5084)) |
| identity + transport | `contacts` — username, wallet address, endpoint URL, shared secret |
| packet integrity | HMAC-SHA256, `hmac.compare_digest` |

Compare what this panel stores: **Common Name, Remote URL, Access Token** — a bare URL and a
bearer token. No username, no wallet, no signed packets, and no permission model on either side.

So the two mechanisms differ in exactly the dimension Steven names. His own phrasing calls for
*contact endpoint/username* — which is Contacts, not this. The design question is whether this
panel should exist at all, or be replaced by (or become a view onto) the contact list.

Worth noting the same duplication shape as UI-027 (two chain servers) and Q-28 (crews vs
topologies): a working subsystem and a parallel unconsumed one covering the same ground.

---

## A23. Confirmed defects — `settings.plugins`

**Steven's label:** `settings.Plugins` — *"This is the MCP area primarily. We need to be able to
add, and edit, as well as remove MCP servers. Not just discover them."*

Two of the three already work. But auditing this panel surfaced something far more important than
the panel itself — see UI-079.

### UI-077 — Add and Remove already work; only Edit is missing
**Severity: medium · Status: open**

The backend has the full set ([server.py:11039-11044](../python/harness/server.py#L11039)):
`mcp.connect_server`, `mcp.list_servers`, `mcp.remove_server`, `mcp.save_servers`,
`mcp.list_tools`, `mcp.call_tool`.

| Operation | Status |
|---|---|
| **Add** | **works** — form submit → `connectServer()`, with stdio/SSE field switching |
| **Remove** | **works** — every server card renders a `Remove` button wired to `mcp.remove_server` ([renderer.js:13175](../renderer/renderer.js#L13175)) |
| List tools | **works** — per-card `List Tools` → `mcp.list_tools` |
| **Edit** | **missing** — `mcp.save_servers` exists on the backend and is **never called from `renderer/`** |

So Steven's ask is narrower than it appears: only edit is absent. To change a server today you must
Remove and re-Add. Note the Remove button only appears *on a card*, and the reference screenshot
shows "No MCP servers registered" — so with an empty list there is nothing to see, which may be why
remove looked absent.

### UI-078 — Plugin permission gating is asserted but unverified
**Severity: medium · Status: open — needs verification**

The panel states *"Plugin permissions remain gated by manifest declarations and gateway policy
before tools can run."* The Test MCP Provider card declares `fs:read` and `shell:execute` — the two
most consequential permissions a plugin could hold.

Given how many "gated" claims in this audit turned out not to be enforced (UI-040, UI-058, UI-044),
this one warrants a direct check of the gateway path before it is trusted. Not asserted as broken —
recorded as unverified.

---

## A24. — RETRACTED: the checkout was simply behind origin

### UI-079 — ~~The live UI matches a rollback snapshot, not the working tree~~ **retracted**
**Status: closed — cause identified, no defect**

> **Retraction (2026-08-23, same session).** This finding originally read *"the running app's UI
> matches a rollback snapshot"* and inferred that a rollback may have silently reverted working-tree
> UI work. **That inference was wrong.** The cause is mundane: the local checkout was **8 commits
> behind `origin/main`**.

The evidence that prompted it: the reference screenshot showed a `Scan for servers` button present
in `.tala_snapshots/checkpoint/renderer/index.html` and absent from `renderer/index.html`.

The actual explanation is an upstream commit the checkout had not pulled:

```
6e78a02 feat: swarm control for both parties, and a scan button for MCP discovery
```

The checkpoint was taken while the tree was at or after that commit; the working tree was behind it.
So the checkpoint was **ahead** of the working tree, not the tree having lost work. After
`git pull --rebase`, `renderer/index.html` contains the button and the discrepancy is gone.

Nothing was reverted, and no rollback occurred. Q-45 ("has a rollback already reverted working-tree
UI?") is answered: **no**.

**What this does not retract.** UI-056 stands on its own mechanism — `resolve_user_data_dir()`
returns `_application_root()`, two complete `nodes/` directories exist, and which root the running
process uses was never confirmed. Q-44 remains the highest-value open question; it is simply no
longer supported by the checkpoint evidence cited here.

---

## A24b. **Audit-wide caveat** — performed against a stale checkout

### UI-094 — Findings were gathered 8 commits behind `origin/main`
**Severity: high (methodological) · Status: open**

The entire audit above was performed against a working tree that was 8 commits behind the remote.
Those commits have since been pulled. They touched code this audit examined:

| Commit | Relevance to findings |
|---|---|
| `2c58af1` feat(mcp): make MCP actually usable, and discover servers already on the machine | `settings.plugins` (UI-077) |
| `6e78a02` feat: swarm control for both parties, and a scan button for MCP discovery | `settings.plugins`, Agent Builder |
| `c7d8447` feat(agents): let Tala author personas and swarms, not just execute them | `settings.agent` (UI-049–051), Agent Builder |
| `2aa1832` fix: research tools withheld, doc validators rejecting their own output | Research Library (UI-018–021) |
| `a4a7da0` fix: three silent-rewrite paths found by the second cloud review | possibly UI-014/015, UI-056 |
| `f510678` fix(swarm): a stalled run and a silent overwrite | Agent Builder swarm findings |
| `e408eae` feat(trajectory): export what actually happened | new `sdk/agent/trajectory.py` |
| `6c0210f` chore: stop tracking setuptools build metadata | — |

Diff scope: `renderer/index.html` +15, `renderer/renderer.js` +167, `sdk/agent/tools.py` +26, plus
three new modules (`agent_authoring.py` 469, `mcp_bridge.py` 292, `trajectory.py` 286).

**Re-verified so far:**

| Finding | Status after pull |
|---|---|
| UI-079 | **retracted** — see above |
| UI-077 (MCP edit missing) | **still valid** — `mcp.save_servers` is still called 0 times from `renderer/` |

Everything else in this document is **unverified against the current tree**. The renderer changes
are modest (182 lines across both files), so most UI findings likely stand — but the four `fix:`
commits above specifically target silent-rewrite and validator behaviour, which is the same
territory as several findings here.

**Before acting on any finding, re-check it against the current tree.** Findings touching MCP,
persona/swarm authoring, and the research tools are the most likely to have moved.

### UI-094 verification pass — results
**Completed 2026-08-23, against `origin/main` @ `6c0210f`**

Re-checked the findings most exposed to the eight missed commits, plus a representative sample of
the rest.

| Finding | Verdict |
|---|---|
| UI-079 (rollback reverted UI) | **retracted** — checkout was behind origin |
| UI-085 (RPC failures not logged) | **evidence corrected, conclusion stands** — 15 `log_to_db` sites, 0 at ERROR, 0 on RPC failure paths |
| UI-014 (`browser.get_dom` stub) | still valid — the "requires IPC callback setup" string is unchanged |
| UI-015 (`browser.execute_js` no listener) | still valid — 0 listeners in `renderer/` |
| UI-018 / UI-019 (search non-200 fallthrough, `/search` substring) | still valid — both patterns unchanged in `search_manager.py` |
| UI-029 (task description unescaped) | still valid |
| UI-028 (tests write the live DB) | still valid |
| UI-045 (`APPLY CHANGES` dead) | still valid — 0 references in `renderer.js` |
| UI-046 (remote provider = key present) | still valid |
| UI-049 / UI-050 (no crew hierarchy) | still valid — the only `parent_id` in `db.py` is `tasks.parent_id` |
| UI-051 (crew fields write-only) | still valid, with a new writer: `agent_authoring.py:133` also INSERTs `crew_type`. Still no behavioural consumer |
| UI-060 (no workflow-execute tool) | still valid — 0 such tools; registry still 66 |
| UI-077 (MCP edit missing) | still valid — `mcp.save_servers` called 0 times from `renderer/` |
| UI-080 (auth stored plaintext) | still valid — 0 encryption calls in the auth save path |
| UI-083 / UI-084 (channels allowlist, no Discord adapter) | still valid — 0 allowlist references, 0 Discord classes |
| UI-091 (hardcoded health literals) | still valid |

**Net effect of the pass:** one finding retracted (UI-079), one finding's evidence corrected with
its conclusion intact (UI-085). Everything sampled above is unchanged by the eight commits. The
`Scan for servers` button is now present in the working tree.

Findings not individually re-checked are lower-severity or in areas the eight commits did not
touch; the caveat above stands for those.


---

## A25. Confirmed defects — `settings.auth`

**Steven's label:** `settings.auth_provider` — *"This allows you to choose a provider for
authentication against other services."*

Four credential types: SSH Private Key, RDP Credentials, API Key, OAuth 2.0. The form is complete
and type-aware. The problem is what the panel claims about how the values are stored.

### UI-080 — "Zero-Trust Database Encryption" — there is no encryption
**Severity: high (security) · Status: FIXED 2026-08-23**

> **Fixed.** auth_credentials secrets are encrypted at rest with AES-256-GCM, keyed from vault/.keys/credential_key.bin, using an envelope so partially migrated tables still work and a failed decrypt returns the row untouched rather than dropping it. All four readers decrypt. Also widened the redaction list: private_key was absent, so the full OPENSSH key block was being sent to the renderer while its passphrase was dutifully redacted.

The panel's stated invariant reads:

> **Zero-Trust Database Encryption** — All credential values are persisted locally inside
> PostgreSQL. Sockets are isolated to secure client-only handshakes.

The heading promises encryption. The sentence only claims *local persistence* — and that is all
that happens. `auth_credentials.config` is plain JSONB
([db.py:327](../python/harness/db.py#L327)):

```sql
config      JSONB       NOT NULL DEFAULT '{}'::jsonb,
```

There is no encryption anywhere in the save path — a search of the auth handlers for
`encrypt` / `fernet` / `aes` / `cipher` returns nothing.

This is the most serious instance of the plaintext family (UI-047, UI-055, UI-070, UI-074) because
of *what this panel specifically collects*:

| Credential type | Stored in clear |
|---|---|
| SSH Private Key | the full `-----BEGIN OPENSSH PRIVATE KEY-----` block, plus its passphrase |
| RDP Credentials | host, username, **password** |
| API Key | service domain + key value |
| OAuth 2.0 | client ID, **client secret**, **refresh token** |

A user reading "Zero-Trust Database Encryption" above a field labelled `PRIVATE KEY BLOCK` will
reasonably conclude the key is encrypted at rest. It is not. The codebase has AES-256-GCM in use
for chain documents and maintains `vault/.keys/` — the primitives are present and unused here.

The second sentence — *"Sockets are isolated to secure client-only handshakes"* — is not specific
enough to verify and was not confirmed either way.

### UI-081 — "Authenticated shell tasks attempt credentials in priority order" is unsupported
**Severity: medium · Status: fixed (2026-08-28)**

The rule now describes what consumes the ordering — search and git providers, by `credential_id`
join — and says explicitly that shell tasks do not: they strip credentials out of the subprocess
environment, which is the opposite of attempting them in order.

**Original finding.**


The stated invariant *"Priority Routing — Authenticated shell tasks attempt credentials in
descending order of user-defined priority"* does not match the shell implementation. `shell.py` and
`async_shell.py` reference credentials only to **scrub them out of the subprocess environment**
([shell.py:38](../sdk/agent/shell.py#L38)) — the opposite of attempting them.

Credentials *are* genuinely consumed, but by three other paths:

| Consumer | How |
|---|---|
| `search_providers` / `git_providers` | `credential_id` join — the good indirection pattern (D13, D15) |
| [tools.py:682](../sdk/agent/tools.py#L682) | Telegram credential lookup by name/type |
| [gates.py:71](../sdk/agent/gates.py#L71) | `AuthorizationGate` — a credential *named* after an action signature with `priority = 1` authorises that action |

That last one is worth flagging on its own: **credential `name` and `priority` are being used as an
authorization allowlist.** Renaming a credential, or reordering the priority chain from this panel,
silently changes what actions the agent is permitted to take — with nothing in this UI indicating
that side effect.

---

## A26. Confirmed defects — `settings.firewall`

**Blockchain Firewall & Trust Matrix.** Of the two stated access-control laws, one works and one
cannot fire.

### UI-082 — Clearance enforcement is inert: no node declares a clearance
**Severity: high · Status: FIXED 2026-08-28**

> **Fixed by composition.** A peer node read now consults `access_grants` first: an explicit
> invitation to a named node supersedes both the contact's `nodes` flag and the firewall
> clearance level, and lockdown short-circuits ahead of either. The clearance machinery is
> untouched and still governs any peer who was not invited — the finding was that clearance
> was the *only* mechanism and nothing could override it, not that it was wrong.


The enforcement code is real and correct
([server.py:5124](../python/harness/server.py#L5124)):

```python
req_clearance = state_data.get("clearance") or state_data.get("clearance_level") or state_data.get("required_clearance")
if req_clearance is not None:
    try: req_clearance = int(req_clearance)
    except (ValueError, TypeError): req_clearance = 0
else:
    req_clearance = 0

if peer_clearance < req_clearance:
    return False, f"clearance_level_insufficient: {peer_clearance} < {req_clearance}"
```

But **0 of 123 nodes declare any of those three keys.** So `req_clearance` is always `0`, and the
comparison becomes `peer_clearance < 0` — never true for any slider value in the advertised range
`[0-10]`.

The panel states: *"Peer clearance level [0-10] acts as a vertical boundary. Reading node values
with higher required clearance will trigger a security violation."* No node has a higher required
clearance, so no violation can occur. **The slider cannot deny anything.**

Note the failure is on the *data* side, not the code side — the reader, the coercion, the default
and the comparison are all correct. What is missing is that nothing ever writes a clearance onto a
node. Any fix belongs in the node schema and authoring path, not here.

---

## A27. Confirmed defects — `settings.channels`

**Steven's label:** *"Channels was something you created. I'm still not sure if I want to have a
proprietary method of communication or not."*

**Scope is undecided — recorded as an open decision (Q-48), not a recommendation.** The findings
below are factual and matter mainly *if* the panel is kept. If it is dropped, UI-083 and UI-084 are
moot and the code should be removed rather than fixed.

### UI-083 — The user allowlist is never enforced
**Severity: high (security) · Status: FIXED 2026-08-23 — conditional on Q-48**

> **Fixed.** Allowlist enforced in BaseAdapter._run, the loop every platform funnels through. Fails closed - an empty allowlist permits nobody. Rejections counted and logged. The panel text that promised the old behaviour was corrected.

The panel collects an `Allowlist: user IDs / usernames` field per channel and warns:

> *"Allowlist — Comma-separated user IDs or usernames. Empty means **everyone who can reach the bot
> can command the agent**. Strongly recommended for Discord servers and group chats."*

`python/harness/channels.py` (281 lines) contains **no reference to `allowlist`** — nor to
`allow`, `authorized`, `permitted`, or `user_id`. The field is collected by the UI, persisted into
`chat_channels.config`, and never read.

So the warning understates the situation. It is not that an *empty* allowlist lets anyone command
the agent — **a populated one does too**, because nothing consults it.

This matters because of what the same panel promises inbound messages can do: *"inbound messages
route through the full agent pipeline (memory, personas, tools) ... channel messages can trigger
agent tools."* An unenforced allowlist in front of tool execution is the most exposed surface found
in this audit.

`Respond to all messages (not just DMs / @-mentions)` is likewise never read.

### UI-084 — Discord is offered in the UI but has no adapter
**Severity: high · Status: open — conditional on Q-48**

`channels.py` defines exactly three classes:

| Class | Line | Status |
|---|---|---|
| `BaseAdapter` | 35 | abstract — `raise NotImplementedError` at line 96 |
| `TelegramAdapter` | 109 | **the only concrete adapter** |
| `ChannelManager` | 182 | registry / lifecycle |

A search across `python/` and `sdk/` for any Discord adapter class returns nothing.

But the UI's platform dropdown defaults to **Discord**, and the setup guide gives detailed,
plausible Discord instructions — create an application at discord.com/developers, enable
`MESSAGE CONTENT INTENT` under Bot → Privileged Gateway Intents, invite with the bot scope. A user
would follow all of that, paste a valid token, click Save, and get a channel the manager has no
adapter to run.

Telegram is the one platform that could actually work.

---

## A28. Confirmed defects — `settings.logging`

**Steven's label:** *"This should log everything. From commands sent to RPC failed, to undetected
port access. It should also be part of the primary method for self diagnosis on Tala."*

The panel itself is well built (D18). Both of Steven's requirements are unmet, and the headline
metrics are misleading.

### UI-085 — RPC failures never reach the audit log
**Severity: high · Status: FIXED 2026-08-23 · Steven's ask #1**


> **Fixed at the one chokepoint.** The hook is in `_err`, not the dispatcher: most handlers
> catch their own exceptions and return `_err` directly, so a dispatcher hook would have
> missed nearly everything. Every RPC error response is now audited, with the failing method
> name carried on a ContextVar so no handler has to pass it. Severity is split -- an internal
> error is `ERROR`, a bad parameter is `WARNING`, because an audit log where a typo looks the
> same as a crashed subsystem is one nobody reads.
>
> Also found: `WARN` and `WARNING` both in use, 98 rows against 16,589. Two spellings of one
> level is a filter that appears to work and silently misses half the rows. Normalised on
> write, either spelling still accepted on read, and `scripts/normalise_log_levels.py`
> migrates the existing rows.

`agent_log` is written by exactly one helper, `db.log_to_db()`
([db.py:995](../python/harness/db.py#L995)).

> **Correction (verification pass, 2026-08-23).** This finding first stated that `server.py` had
> **0** `log_to_db` call sites. That was my error — I searched for a helper named `log_event`, which
> does not exist. The real count is **15**. The conclusion survives, but the evidence below is the
> corrected version.

| Level written from `server.py` | Count |
|---|---|
| `INFO` | 11 |
| `WARNING` | 4 |
| **`ERROR`** | **0** |

So `agent_log` *is* written from the harness — but **never at ERROR level**, and never from an RPC
failure path. `server.py` contains **257** `logger.error/exception/warning` calls, and **none** is
followed by a `log_to_db` write: every RPC handler ends with `logger.exception("<method> error")`
going to the Python logger only, which this panel does not read.

This explains the metrics on screen more precisely than the original version did. `ERRORS / WARNS
1 / 16681` is what you get when the harness writes only INFO and WARNING to this table — the lone
ERROR did not come from `server.py` at all.

So "from commands sent to RPC failed" is the exact gap: **RPC failures are the thing least likely
to appear here.**

Port access is not logged either, and cannot be — no port monitoring concept exists anywhere in the
codebase (established in UI-063).

### UI-086 — Tala cannot read her own logs or health
**Severity: high · Status: FIXED 2026-08-23 · Steven's ask #2**


> **Fixed.** `read_own_health`, `read_own_logs` and `diagnose_self` in
> `sdk/agent/self_diagnosis.py`. Health returns the problems first rather than twelve rows
> of green with one amber buried in them, and each carries its evidence.
>
> **Read-only, deliberately** (Q-19: *"She cannot clear her logs but I should be able too"*).
> Nothing here archives, deletes or truncates, and a test asserts that by walking the AST
> rather than grepping, so the module can go on describing the RPCs it does not call.
>
> An empty log read reports "nothing matched the filter, not necessarily that nothing went
> wrong" -- which is exactly what UI-085 was.

Four logging RPCs exist — `logging.get_events`, `logging.get_health`, `logging.archive_events`,
`logging.clear_events` — and all four are **UI-only**. None is registered as an agent tool.

Of the 66 tools in the registry, none reads logs, health, or diagnostics (the sole near-match,
`run_security_classification_audit`, is unrelated). There is also no generic RPC-invoke tool
(established in UI-060).

So *"part of the primary method for self diagnosis on Tala"* has no mechanism. She cannot see the
event stream, cannot read `OVERALL: DEGRADED`, and cannot enumerate which of the twelve subsystems
are amber. Every signal on this screen is visible to Steven and invisible to Tala.

Same shape as UI-038 (vault) and UI-060 (workflows): the data exists, the RPC exists, the tool does
not.

### UI-087 — "PROMPT AUDITS" is a substring grep, not an audit count
**Severity: medium · Status: fixed (2026-08-28)**

Two bugs, and the second was mine.

**The tile counted a substring.** `message LIKE '%guardrail%'` over `agent_log` — free text, not
records. Worse, `GuardrailsManager` anchored violations to the chain and wrote nothing to
`agent_log` at all, so the tile read `0` regardless of how much validation ran. It now counts a
structured marker, `trace->>'event_type' = 'guardrail-violation'`, and `_log_violation_to_ledger`
writes one. A message somebody rewords no longer changes a number.

**And then it still read zero.** `log_to_db` called `json.dumps(trace)` before passing it to a
column whose connection codec *already* encodes with `json.dumps` — so `agent_log.trace` held a JSON
**string**, `jsonb_typeof` returned `'string'`, and every `trace->>'key'` silently returned NULL.
The replacement query would have shipped as a quieter version of exactly the bug it was fixing. Root
cause fixed in `log_to_db`; the tile reads through both shapes so history written before the fix
still counts.

This is the `tasks.payload` double-encoding pattern in a second column. **Any JSONB column written
through this codec is suspect**: if a call site encodes before passing, `->` and `->>` against it
fail silently rather than erroring.

**Original finding.**


The `PROMPT AUDITS 0` tile is computed as
([server.py:8073](../python/harness/server.py#L8073)):

```sql
SELECT COUNT(*) FROM agent_log
WHERE message LIKE '%Guardrail%' OR message LIKE '%guardrail%'
```

It counts log rows whose **free-text message happens to contain the word "guardrail"**. It is not
counting audit records — no prompt-audit record type exists. Given UI-085, guardrail activity would
have to be written via `log_to_db` to be counted at all, and `GuardrailsManager` does not do that.

The tile will therefore read `0` regardless of how much prompt validation actually occurs. Another
instance of the substring-heuristic pattern (UI-019, UI-087).

### Observation — the `LEVEL` column renders empty
**Severity: low · Status: open**

Every visible row in the log table shows a blank `LEVEL` cell, despite `level` being a real
`agent_log` column that the metrics queries filter on (`WHERE level = 'ERROR'`). The severity
filter buttons (ERROR / WARN / INFO / DEBUG / ERRORS ONLY) presumably work off the same field, so
the value exists — it simply is not rendered into the row.

---

## A29. Confirmed defects — `settings.architecture`

**Steven's label:** *"How the data works and flows. This should be able to be used as a flowchart
for project proposal of Tala, and how it works."*

Because the stated purpose is **external presentation**, accuracy is the requirement, not polish.
Audited each of the nine boxes against the code.

### UI-088 — Box-by-box accuracy audit
**Severity: high (for proposal use) · Status: open**

| Box | Subtitle | Accurate? |
|---|---|---|
| Tala Renderer | Electron Frontend UI & Context Canvas | **yes** |
| Harness Engine Room | Python Async Server & JSON-RPC Router | **yes** |
| ChromaDB Node Index | Local Semantic RAG & Vector Embeddings | **yes** — `retrieval.py` imports `chromadb` with a persistent client |
| Workflow Builder | Visual State-Machine Pipeline Compiler | **yes** — the builder and `workflow_compiler.py` are real |
| PostgreSQL **Vector DB** | Neural Nodes, Profiles & Auth Keys | **no — see UI-089** |
| Workflow Executor | State-machine steps & Script Runner | **partial** — executes, but nothing can *invoke* it (UI-060: no agent tool, only `workflow.execute` RPC) |
| Blockchain Firewall | Clearance Levels & Category Access checks | **partial** — category checks work; **clearance is inert**, no node declares one (UI-082) |
| P2P Mesh Router | Peer Node Handshakes & Remote Instances | **partial** — handshakes are real and signed; "Remote Instances" is the unconsumed `runtime_instances` table (UI-075) |
| Inference Engine | vLLM Streams & Cloud API Routing | **partial** — cloud routing works; vLLM is an *offer-to-install* in the Engine Manager, not a running component, and is Linux/WSL-only on this platform |

Four of nine boxes describe capabilities that do not work as depicted. For an internal orientation
diagram that is tolerable; for a proposal it is the difference between describing a system and
describing an intention.

### UI-089 — "PostgreSQL Vector DB" is not a vector database
**Severity: medium · Status: fixed (2026-08-28)**

Relabelled **PostgreSQL — operational state & full-text search · profiles · credentials**. There is
no pgvector and no vector column type; Postgres provides `tsvector`, which is full-text search and a
different thing from embeddings. Every embedding lives in ChromaDB, which the diagram already draws
as its own box, so the old label handed ChromaDB's job to PostgreSQL.

**Original finding.**


There is **no pgvector** — no `CREATE EXTENSION vector`, no vector column type anywhere in
[db.py](../python/harness/db.py). What PostgreSQL provides is `tsvector`
([db.py:618](../python/harness/db.py#L618)) — Postgres **full-text search**, which is a different
thing from vector embeddings.

All vector/embedding work happens in ChromaDB, which the diagram already shows as a separate box.
So the label attributes ChromaDB's role to PostgreSQL and puts two different retrieval technologies
under one name.

The "Neural Nodes" half is also imprecise. A `nodes` table does exist
([db.py:60](../python/harness/db.py#L60)) — `id UUID, node_type, execution_authority,
content_json JSONB` — but the **canonical** HTML-NN store is the filesystem: 123 `.html` files, and
the profile load path states outright that *"the node is the canonical, single source of truth …
not the profiles table"* (UI-056). A reader of this diagram would conclude nodes live in Postgres.
They live on disk.

Accurate labelling would be closer to: **PostgreSQL — Relational Store & Full-Text Index**
(profiles, auth, ledger, tasks, logs), with a separate **Filesystem — HTML-NN Node Store**. The
absence of that third box is arguably the diagram's largest omission, since the node store is the
project's defining idea.

### UI-090 — The diagram is static markup and cannot drift
**Severity: medium · Status: fixed (2026-08-28) — as labelling, not as a generator**

Retitled **Architecture Overview**, with a note that it is hand-maintained and not derived from the
code. It was titled a *Data Flow Map* while carrying undirected, unlabelled edges — no reader could
tell what calls what, in which direction.

Deriving it from the codebase is a real build and was not attempted. What is fixed is the part that
made it dangerous: it claimed to be something it could not be, so it could only fall out of date
silently. It now says so.

**Original finding.**


The component graph is hardcoded in `index.html`
([index.html:2208](../renderer/index.html#L2208)); `renderer.js` only shows/hides the panel. Nothing
derives it from the codebase, so it cannot fall out of date loudly — it can only fall out of date
silently, which is what has happened.

Also relevant to the stated purpose: the boxes are connected by faint undirected lines with **no
edge labels and no arrowheads**. The panel is titled *"Component Graph & Data Flow Map"*, but no
data flow is actually expressed — a reader cannot tell what calls what, or in which direction. For
"how the data works and flows" in a proposal, the edges carry most of the meaning and are currently
the least specified part.

---

## A30. Confirmed defects — `settings.about`, and a correction to `settings.logging`

**Steven's label:** *"This is only about the software and not about the LLM or RP personality. It
should be a standard about page. Core Capabilities can grow, but other than that nothing changes."*

Scope accepted — a static about page is the right design. Two things are wrong within that scope,
and one check here overturned an earlier entry.

### UI-091 — 9 of 12 subsystem health indicators are hardcoded constants
**Severity: high · Status: FIXED 2026-08-23 · Corrects D18**


> **Fixed.** All twelve indicators are measured, and each returns *why*: "8 of 8 providers
> online", "88 tools registered", "no errors in the last 5 minutes". Where nothing can be
> measured the answer is `UNKNOWN`, never `HEALTHY`, and an `UNKNOWN` present makes OVERALL
> `PARTIAL` rather than rounding up. Two probes returned `UNKNOWN` on first run and both
> were real bugs it had surfaced rather than hidden: the memory manager singleton is
> `get_manager()`, not `MemoryManager.get()`, and `tasks` has no `updated_at` column.
>
> A probe that raises is caught and reported as `UNKNOWN` — the screen you check when things
> are broken has to survive things being broken. A test caught that the first version
> evaluated probes at the call site, so a synchronous one that raised escaped the guard.

Checking the `Reflection System` capability claim led to
[server.py:8150-8218](../python/harness/server.py#L8150). Of the twelve indicators on the LOGGING
panel:

| Computed from real state | Hardcoded literal |
|---|---|
| `DB_HEALTH_SERVICE` — from a DB probe | `MCP_TOOL_AVAILABILITY` = "HEALTHY" |
| `INFERENCE_SERVICE` — from provider count | `SEARCH_RETRIEVAL_SERVICE` = "HEALTHY" |
| `POLICY_GATE` — from the guardrails category | `MEMORY_AUTHORITY_SERVICE` = "HEALTHY" |
| | `TOOL_EXECUTION_COORDINATOR` = "HEALTHY" |
| | `QUEUE_BACKLOG_PRESSURE` = "HEALTHY" |
| | `BOOTSTRAP_RUNTIME_SERVICES` = "HEALTHY" |
| | `AUTONOMY_ORCHESTRATOR` = "MAINTENANCE" |
| | `REFLECTION_SERVICE` = "DEGRADED" |
| | `DASHBOARD_TELEMETRY` = "DEGRADED" |

```python
# 11. REFLECTION_SERVICE
services["REFLECTION_SERVICE"] = "DEGRADED"
```

So `OVERALL: DEGRADED` is not a diagnosis. It is `DEGRADED` because two constants say so, and it
will say `DEGRADED` forever regardless of actual state — while six subsystems will report `HEALTHY`
forever, equally regardless.

This is worse than the hardcoded badges elsewhere (UI-064, UI-073, UI-039, UI-017), because those
assert *health* and would at least be noticed when something breaks. These assert a permanent
mixture, so the panel looks diagnostic while telling you nothing. It also undercuts Steven's
logging ask directly: a health surface that cannot change cannot support self-diagnosis.

### UI-092 — The About page reports `v0.0.0`
**Severity: medium · Status: fixed (2026-08-28)**

The version sits in `<span id="about-version">` and the updater's selector includes it, so the About
page shows the running version instead of a hardcoded `v0.0.0` stale by 183 patch releases.

The updater also asked once instead of once per element — it made the same RPC call for every match
to get the same answer.

**Original finding.**


`index.html:2349` hardcodes the version into a text node:

```html
<div class="about-brand-meta">Autonomous Intelligence System &nbsp;&middot;&nbsp; v0.0.0</div>
```

`package.json` says `0.1.83`, and the status bar shows `v0.1.83` correctly — `renderer.js:97`
populates `#app-version, #status-version` at runtime. This element has **no id**, so the updater
cannot reach it.

On a page whose entire job is to state what the software is, the version is the one field that must
be right, and it is the one field that is stale by 183 patch releases.

Related and lower severity: `FOUNDED 2023` is presumably intentional, but `© 2023 T.A.L.A. System
Architecture` in the footer is three years stale as of this audit.

### UI-093 — Two Core System Capabilities are not currently true
**Severity: medium · Status: fixed (2026-08-28)**

Now reads **Memory: HTML-NN nodes with ChromaDB retrieval**.

`mem0` *is* installed since this was filed, which is not the same as used: nothing constructs
`Mem0ReadOnlyAdapter` in any live path, and the adapter blocks writes by design, so "mem0
(short-term)" was still untrue — for a different reason than when it was written. A test asserts
nothing constructs it, so the claim can come back if that changes.

**Original finding.**


Since capabilities is the one section that grows, its accuracy is the thing to keep correct.

| Capability | Status |
|---|---|
| Hybrid LLM Inference (Local + Cloud) | **true** |
| Astro-Emotional Engine (Dynamic personality) | **true** — birthdate → `EphemerisEngine` → `StyleModulator`, with `pyswisseph` present (UI-066) |
| **Dual Memory: mem0 (short-term) + RAG (long-term)** | **half true** — RAG via ChromaDB is real; **`mem0` is not installed**. It is an *optional* extra in `pyproject.toml` (`mem0 = ["mem0ai>=0.1.29"]`) and `import mem0` fails in the active venv. The adapter that exists, `Mem0ReadOnlyAdapter`, blocks writes by design |
| **Reflection System (Self-improvement loop)** | **unverified** — no `ReflectionEngine` class or `reflect()` function exists in `sdk/` or `python/`; the only occurrence of "REFLECTION" is the hardcoded `"DEGRADED"` literal above |
| Browser Perception & Automation | **true, with the caveat in UI-013** — Playwright works but drives an invisible browser |
| MCP Server Ecosystem | **true** — add/remove work, edit missing (UI-077) |
| Guard Builder + Custom Validator Builder | **true** — guardrails are genuinely enforced (D12) |

### UI-095 — Q-44/Q-30 diagnostic: the portable data root works; one leak remains
**Severity: low · Status: mostly resolved**

**Steven's stated intent:** *"This directory should be the directory TALA is installed to. The
intent is for TALA to be self sustaining and work from her own directory, this way she could be
installed on a USB stick and moved."*

**That intent is implemented and currently working.** `TALA_USER_DATA` is unset at process, user and
machine scope, so every resolver falls through to the install directory:

| Resolver | Expression |
|---|---|
| `main.js` `resolveDataRoot()` ([:101](../electron/main.js#L101)) | `TALA_USER_DATA \|\| appPath` |
| `ipc-handlers.js` `userData` ([:180](../electron/ipc-handlers.js#L180)) | `TALA_USER_DATA \|\| appPath` |
| Python `resolve_user_data_dir()` ([:109](../sdk/agent/documentation_nodes.py#L109)) | `TALA_USER_DATA \|\| _application_root()` |

They cannot drift apart by accident: `firstRunEnv()`
([main.js:106](../electron/main.js#L106)) injects `TALA_USER_DATA: dataRoot` into the spawned Python
process, so the harness is told explicitly which root to use rather than deriving its own.

**Filesystem evidence — the live root is the checkout:**

| Directory | HTML nodes | Newest write |
|---|---|---|
| **`nodes/` (install dir)** | **123** | **2026-08-23 10:39** — live |
| `.tala_snapshots/checkpoint/nodes/` | 122 | 2026-08-23 08:39 — this morning's snapshot |
| `%APPDATA%/tala-engine/nodes/` | 55 | 2026-08-07 — 16 days stale, pre-portability legacy |
| `dist/win-unpacked/.../nodes/` | 7 | 2026-06-08 — stale packaged build |

Only one root is live, and it is the right one.

**Consequence for UI-056.** The data-root divergence hypothesis is substantially weakened — there is
no second active root to lose writes to. UI-056's *mechanism* remains real (the canonical store is a
file at a runtime-resolved path), but on this machine, as configured today, it is not diverging.
Whatever caused *"after a few restarts it goes away"* is more likely the incomplete DB sync
(the profile fields the sync never writes) than a moving directory. See UI-056's field list.

### UI-096 — `electron-store` still writes outside the portable root
**Severity: low · Status: open**

One component ignores the resolver and hardcodes Electron's own userData path
([main.js:207](../electron/main.js#L207)):

```js
store = new Store({
  // Always store data in userData to keep it isolated from project source
  cwd: app.getPath('userData'),
  name: 'tala-config',
```

That resolves to `%APPDATA%/tala-engine/tala-config.json` and currently holds:

```json
{ "windowBounds": { "x": 0, "y": 0, "width": 2048, "height": 1032 },
  "lastView": "neural-node" }
```

Low impact — window geometry and last-open view, no user data. But it is a genuine portability
leak against the stated intent, and the comment above it (*"Always store data in userData to keep
it isolated from project source"*) states the **opposite** policy to the one the rest of the app
now follows. Two installs on one machine would also share this file.

`ipc-handlers.js` even warns about exactly this class of mismatch: *"Must match main.js's
resolveDataRoot() exactly, or the app, the harness, and this sandbox end up looking at different
directories."*

### UI-097 — MCP server discovery is Windows-only and fails silently elsewhere
**Severity: high · Status: open · Raised by Steven, 2026-08-23**

Steven's concern — *"%APPDATA% is specific to windows and may not work on linux or mac"* — is
correct, and the place it actually bites is the **newly-shipped MCP discovery** feature
(`2c58af1`, one of the eight commits this audit originally missed).

`_DISCOVERY_LOCATIONS` ([mcp_bridge.py:193](../sdk/agent/mcp_bridge.py#L193)) uses Windows `cmd`
variable syntax and backslash separators throughout — five entries keyed on `%APPDATA%` or
`%USERPROFILE%`, plus two project paths written with backslashes.

Expansion is `os.path.expandvars(raw_path)` ([:253](../sdk/agent/mcp_bridge.py#L253)). **On POSIX,
`expandvars` expands `$VAR` and `${VAR}` only — it does not understand `%VAR%`.** The string passes
through unchanged, `Path()` treats it as a relative path whose first component is the literal text
`%APPDATA%`, and the file is never found.

Effect per platform:

| Location | Windows | Linux / macOS |
|---|---|---|
| 5 `%APPDATA%` / `%USERPROFILE%` entries | resolve | **literal, never match** |
| the two backslash-separated project paths | resolve | **backslash is a filename character on POSIX** — never match |
| `.mcp.json` | resolves | resolves |

So on Linux and macOS **7 of 8 discovery locations can never match**, and the failure is invisible:
finding zero servers looks exactly like having zero servers installed. The `Scan for servers` button
would appear to work and always come back empty.

The correct paths also differ per OS — Claude Desktop is
`~/Library/Application Support/Claude/` on macOS and `~/.config/Claude/` on Linux — so this needs a
per-platform table, not just syntax repair.

### UI-098 — Two comments describe an APPDATA fallback that was deliberately removed
**Severity: low · Status: open**

The portable resolver is correct (UI-095), but two comments in `server.py` describe the *old*
behaviour as if it were current:

- [server.py:517](../python/harness/server.py#L517) — *"the single canonical resolver ... (TALA_USER_DATA override -> **APPDATA if set** -> a directory relative to the application's own install location)"*
- [server.py:9075](../python/harness/server.py#L9075) — *"falls back to the standard **TALA_USER_DATA/APPDATA/app-relative** resolver"*

`resolve_user_data_dir()` has **no APPDATA step**. Its own docstring records why it was taken out:
*"APPDATA is set on essentially every real Windows machine, so auto-preferring it silently defeated
portability by default there — exactly the case that matters most, since Windows is the common
USB-boot target."*

Harmless at runtime, but both comments assert the exact Windows-specific behaviour the design
rejects, and would mislead the next person checking whether the app is portable.

### UI-099 — The live profile node is in the legacy flat format, so most fields cannot load
**Severity: critical · Status: FIXED 2026-08-23 · Answers Q-55 · Corrects UI-056**

> **Fixed.** Node regenerated through the real writer. concept-user-profile.html now carries state_data.config with all 10 sections - roles(10), skills(5), projects(4), connections(1), privacy(6) - and passes schema validation.

Tracing Steven's answer to Q-55 ("sync should write everything") found the actual mechanism, and it
is **not** the DB sync.

**Two different save endpoints exist, and I attributed the bug to the wrong one:**

| Endpoint | Fields accepted | Node format written |
|---|---|---|
| `identity.save_user_profile` ([server.py:4027](../python/harness/server.py#L4027)) | **13 flat params only** — name, birthdate, tone, biography, 4 RP, 5 PII | flat legacy keys |
| **`profiles.save`** ([server.py:6250](../python/harness/server.py#L6250)) | **full nested config** | `state_data.config` = full config |

> **Correction to UI-056.** That finding blamed the DB sync inside
> `handle_identity_save_user_profile` for dropping fields. The USER PROFILE panel does not call that
> handler — `saveActiveProfile()` calls **`profiles.save`** and sends the complete nested config
> including `roles`, `skills`, `projects`, `connections` and `privacy`. The renderer is correct and
> the narrow endpoint is not in its path.

**The real defect is the state of the live node.** `identity.get_user_profile`
([server.py:3885](../python/harness/server.py#L3885)) loads the profile like this:

```python
config = sd.get("config")
if not isinstance(config, dict):
    # flat legacy keys, not a "config" object. Reconstruct a
    # best-effort config from them so old nodes still render
```

Reading the live file confirms which branch runs:

```
nodes/concept-user-profile.html  ->  state_data keys:
['name','birthdate','tone','biography','rp_name','rp_class','rp_faction',
 'rp_backstory','pii_legal_name','pii_dob','pii_email','pii_phone','pii_registries']
has config dict: False
```

**The node is in the flat legacy format.** So every load takes the "best-effort, always-incomplete"
branch, and roughly 22 fields have nothing to load from — Roles & Work entirely, Skills, Projects,
Connections, the privacy block, and most of Core Identity and About.

That is exactly the reported symptom: fill in Roles & Work, and it is gone after a reload. Not
because the sync dropped it, but because the node on disk has no slot for it.

Nothing in the current codebase calls `identity.save_user_profile` — the grep hits are its own
definition, docstring, log label and one doc reference. So the flat node predates the switch to
`profiles.save` and has simply never been replaced.

**Open sub-question:** if the panel calls `profiles.save`, and that writes `state_data.config`, why
is the node still flat? Either `profiles.save` has not completed successfully since the format
change, or its node-write path is not being reached. Worth confirming before fixing, since a
migration alone would not help if the writer is broken.

### UI-100 — Q-57 answered: the node write fails silently and the loader never falls back to the DB
**Severity: critical · Status: FIXED 2026-08-23 · Data was NOT lost**

> **Fixed.** This finding is the *diagnosis*; the three composing defects were fixed
> under UI-056 (swallowed node write), UI-099 (loader had no DB fallback) and UI-085
> (warnings never reached `agent_log`). Kept as the write-up of the whole mechanism.

Ran with the app live. The complete mechanism:

**1. The database has the full profile.** `profiles.config` for `master-user` contains every
section, populated:

| Section | Entries |
|---|---|
| `identity` | 12 |
| `roles` | 10 |
| `privacy` | 6 |
| `about` | 5 |
| `skills` | 5 |
| `projects` | 4 |
| `connections` | 1 |

So `profiles.save` works, and **Steven's profile data is intact and recoverable.**

**2. The node write is wrapped in a swallowing try/except.**
[server.py:6511](../python/harness/server.py#L6511):

```python
node_file = nodes_dir / "concept-user-profile.html"
node_file.write_text(html, encoding="utf-8")
logger.info("Synced user profile node: %s", node_file)
except Exception as sync_exc:
    logger.warning("Failed to sync profiles save to HTML Neural Node: %s", sync_exc)
```

A failure here logs a warning and `profiles.save` still returns success. The UI reports the profile
saved, because as far as the DB is concerned it was.

**3. The loader reads only the node, never the DB.** `identity.get_user_profile` branches on
`sd.get("config")`; the live node has no `config` key, so every load takes the
"best-effort, always-incomplete" reconstruction path (UI-099).

**Timeline evidence:**

| Fact | Value |
|---|---|
| `profiles.updated_at` (last successful save) | 2026-08-07 19:33 |
| node last committed, in flat format | 2026-08-12 18:51 (`cfb57de`) |
| node vs `HEAD` | **identical** — nothing has rewritten it since |
| `agent_log` rows mentioning profile / Neural Node | **0** |
| harness log files on disk | **none** |

So the node has not been successfully written since before it was committed, and **the failure left
no trace on any surface** — not the UI, not `agent_log`, not a log file. That is why it presents as
"it just goes away".

**Net:** three independent defects compose into the reported bug — a swallowed write (this
finding), a loader with no DB fallback (UI-099), and a logging path that never records warnings
(UI-085). Fixing any one of them alone would leave the bug partly present.

**Recovery:** the DB copy is complete. Regenerating the node from `profiles.config` restores
everything without data entry.

### UI-101 — The access-control model Steven specified, vs what the schema can express
**Severity: high (design) · Status: FIXED 2026-08-28 · Source: P1 answers, 2026-08-23**


> **The schema exists now.** `access_grants` is `(principal_kind, principal_id, resource,
> permission, constraints)` — the resource dimension the nine booleans on a contact could not
> express. Identity and access are separate: being a contact grants nothing, and a grant
> names one resource.
>
> Constraints carry the rest of what Steven specified: *"that agent can only access a certain
> search API, on a certain port, at a single allocated time."* Ports, hosts, an hours window
> that may cross midnight, a start and an expiry. **An unrecognised constraint denies** — a
> constraint nobody evaluates is worse than none, because it reads as a restriction that is
> not there.
>
> Lockdown beats every grant, and not knowing whether lockdown is on is not permission to
> proceed. The individual call sites (UI-040, UI-041, UI-058, UI-082) still each need
> pointing at this; the model they were partial views of now exists.

The six P1 answers define one coherent model. Recorded here because several findings
(UI-040, UI-041, UI-058, UI-081, UI-082) are all partial views of the same missing thing.

| Principle | Source | Current code |
|---|---|---|
| Identity and access are separate — being a contact grants nothing | Q-22 | **not expressible** — `contacts.permissions` is 9 booleans on the *contact*, with no resource dimension |
| Access is granted per resource, by invitation | Q-22 | not implemented |
| A shared item carries its own scope: `global` or `contact-only` | Q-23 | not implemented — one global `skills` flag |
| Callers are identified before being permitted | Q-21 | `p2p.marketplace_search` takes no caller identity |
| Node sensitivity comes from a classification group | Q-46 | enforcement reads node tags already; taxonomy absent |
| Lockdown overrides every grant | Q-32 | concept absent entirely |
| Credential priority is authentication fallback only | Q-47 | `gates.py` uses it as authorization |

**Precedence** (Q-32), highest first: **lockdown** → **contact grant** → **profile visibility**.

**Schema implication.** `contacts.permissions` cannot carry this. A grant needs at minimum a
*(grantee, resource, scope)* triple, where resource is a file / node / skill and scope is
`global` or `contact-only`. The nine-boolean column is a contact-scoped role model, which Q-22
explicitly rejects.

**One piece already fits.** `_peer_can_resolve_node_reference`
([server.py:5084](../python/harness/server.py#L5084)) already reads a node's own tags and
categories to decide access. Classification (Q-46) can ride that existing path rather than needing
a new field — the enforcement is built, only the taxonomy is missing.

**Two P2 questions are now largely pre-answered.** Q-35 asks whether guardrails should absorb
contacts-permissions and firewall into one policy object — the model above *is* that object. Q-36
asks what a policy's subject is — Q-22 answers it: a **(grantee, resource)** pair, not a contact
and not an agent. Both should be revisited rather than reopened.

### UI-102 — Network isolation is never enabled, so `vault:read` can never succeed
**Severity: high · Status: FIXED 2026-08-23 · Found while answering Q-59**


> **Fixed by moving the control.** The flag was permanently false, so `vault:read` could
> never succeed -- but gating *reads* on global network isolation was the wrong control
> anyway: it would have made the vault unusable whenever the app was online, which is
> always. "Nothing gets shared outside" is a property of the **exit**.
>
> Steven settled the four edges, and they do not get the same answer: remote models are
> refused unless that document carries an explicit grant; local search indexes vault bodies;
> backup and sync never see the vault, sealed or otherwise, and the Chroma directory is
> excluded with it because it holds unsealed bodies; contacts get access only by per-document
> invite, deliberately not derived from their general permissions, so a misconfigured
> contact permission cannot disclose a private document.
>
> Every check fails closed. Not knowing which provider is active is not permission.

Steven's lockdown definition — *"it hardlines a network disconnect so no traffic can act, only
conversation between Harness and LLM"* — describes a mechanism that is **already half-implemented
and currently stuck closed**.

The flag exists and is read:

| Line | Code |
|---|---|
| [main.js:193](../electron/main.js#L193) | `let networkIsolated = false;` |
| [main.js:458](../electron/main.js#L458) | `networkIsolated = false;` — on WebSocket connect |
| [main.js:1079](../electron/main.js#L1079) | `isNetworkIsolated: () => networkIsolated` |

**Nothing anywhere sets it to `true`.** It is initialised false and reset to false; there is no
third assignment.

The consequence lands on the vault. `vault:read`
([ipc-handlers.js:386](../electron/ipc-handlers.js#L386)) refuses unless isolation is active:

```js
if (typeof isNetworkIsolated === 'function' && !isNetworkIsolated()) {
  throw new Error('vault:read blocked: network connections are active. ...');
}
```

Since the flag can never be true, **`vault:read` always throws**. This sharpens UI-036/UI-037: the
vault is not merely missing its UI — its one working backend capability is permanently unreachable.
And it sharpens UI-039: the `Network Isolated` badge is not just hardcoded, it asserts a state the
system has no way to enter.

**Good news for the build.** Lockdown does not start from nothing. The flag, the guard, and the
enforcement point all exist — what is missing is a setter and a trigger. That is a much smaller job
than the Q-59 answer implies.

**Resolved (Q-62).** Steven: *"the lockdown overrides forced model and drops to local only
models."* Lockdown sits one tier above the `FORCE` flag and filters the provider chain to
`type in (local, embedded)`, so outbound inference is never permitted and "no traffic can act" holds
literally. `embedded-builtin` is `locked` and always available, so this can never leave the system
without a model.

Two notes for whoever builds it:

- The hook point is narrow — provider selection already honours `config.is_forced`
  ([server.py:2433](../python/harness/server.py#L2433)) over a priority-ordered chain, with the
  embedded engine as documented last resort. Lockdown is one more tier above that.
- **Interacts with UI-046.** Remote providers show `ONLINE` purely because an API key string is
  present. Under lockdown they are unreachable by design yet would still render green.

### UI-103 — `AuthorizationGate` is never invoked, and the emergency halt is never checked
**Severity: high · Status: FIXED 2026-08-28 · Found while answering Q-61**


> **Fixed.** `AuthorizationGate.request_authorization` now resolves against the grant model
> and has a caller. The `priority = 1` lookup is **gone entirely** rather than tightened —
> any credential that happened to sort first authorised any action, which was never the
> intent and is not recoverable by improving the query. The emergency halt still runs first
> and still wins, and the gate records `last_reason`, because a denial the caller cannot
> explain is the same problem as a silent failure.

Two adjacent safety primitives in `sdk/agent/gates.py` are both built and both inert.

**`AuthorizationGate.request_authorization()` has no callers.** A search across `python/` and
`sdk/` returns only its own definition. So the `priority = 1` authorization bug (UI-081) is
**latent, not active** — nothing is being wrongly authorized today, because nothing asks.

Its existing shape is a good fit for the model Steven specified, which is worth noting before
anyone redesigns it:

```python
async def request_authorization(self, action_type: str, resource: str, risk_class: str) -> bool
```

- already **resource-scoped**, matching the *(grantee, resource)* subject from Q-22
- risk tiers already branch: `low` auto-allows, `high`/`critical` demand proof, `medium` defaults
  open
- a `register_pre_approval()` path already exists for granting a specific `action:resource`

**`RevocationHalt` is triggerable but never consulted.** `trigger_emergency_halt()` *is* called —
from `task.control` when the action is `abort`
([server.py:9591](../python/harness/server.py#L9591)). But `is_halted()` has **no callers
anywhere**, so tripping the halt sets a flag nothing reads. Aborting a task raises an emergency halt
that stops nothing.

**Relevance to lockdown (Q-59).** `RevocationHalt` is a second half-built kill-switch primitive
sitting beside `isNetworkIsolated()` (UI-102), which is also never enabled. Two independent halt
mechanisms exist, neither functional. Worth deciding whether lockdown reuses one, or whether the
other should be removed rather than left as a safety control that does nothing.

### UI-104 — Crews are an org structure, not a swarm front-end; and the naming already conflicts
**Severity: high (design) · Status: built (2026-08-28) · Source: P2 answers, 2026-08-23**

**Steven refined the taxonomy on 2026-08-28, and the refinement changes the answer.** The earlier
entry below has the axis as headcount — "many agents one job" versus "many jobs many agents". It is
not. It is **control flow**:

| | Crew | Swarm |
|---|---|---|
| topology | structured — linear pipelines or explicit manager/worker trees | decentralised mesh — peer-to-peer routing, event-driven graphs |
| control flow | **deterministic**, defined upfront | **emergent** — agents trigger handoffs as conditions evolve |
| roles | deep personas, backstories, fixed responsibilities | narrow utility functions, tool interfaces |
| best for | predictable multi-stage pipelines, document generation | open-ended exploration, dynamic dispatch |

Under that definition the naming conflict resolves differently than Q-64 assumed. `Code Repair Swarm`
— a fixed five-role DAG defined upfront and executed deterministically — is unambiguously a **crew**.
And nothing in the codebase routes emergently, so the real swarm is **not misnamed, it is unbuilt**.
The fan-out work of 2026-08-21 made the engine branch; the nodes still carry roles fixed in advance.

**What was built.** The organisation itself: crews as entities, teams inside departments, reporting
lines, and the two things a hierarchy is *for* — routing work into a crew and escalating it upward
(UI-049, UI-050, UI-051). It is deliberately deterministic throughout, which is what makes it a crew
engine rather than a swarm one.

**Still open.** The existing topology engine keeps the name `swarm` in its module, RPC family and
UI, while being crew-shaped by this definition. Renaming it is vocabulary churn across working,
tested code and was not attempted here; it is recorded so the mismatch is deliberate rather than
forgotten. A genuine swarm — emergent handoffs — remains unbuilt.

**Original finding.**


> **Steven (Q-28):** *"Crews are considered employees... Treat it like a business or a military or a
> guild. It is an organization piece. Completely separate from swarms. **Swarms are many agents
> doing the same exact job. Crews are many jobs, many agents.**"*

**This overturns the recommendation in Q-28's original entry.** I suggested crews might be a
friendlier front-end onto swarm topologies to avoid duplicating a working engine. Steven's
distinction is cleaner and the two are genuinely different things:

| | Swarm | Crew |
|---|---|---|
| shape | many agents, **one** job | many agents, **many** jobs |
| purpose | parallelism | organisation |
| structure | flat fan-out | hierarchy: worker -> architect -> manager |
| grouping | none | teams (~5), then departments |

**Hierarchy is deep and real** (Q-29): *"There could be 200 crew members broken into teams of 5, and
each team will report to their respective department."* Reporting is both ownership and upward work
routing. The `agents` table has none of it — no `parent_id`, no team, no department.

**The naming already conflicts, and this needs settling before either is built (Q-64).** By Steven's
definition, the existing topologies are **crew-shaped, not swarm-shaped**: `Code Repair Swarm` is
`planner -> coder -> tester -> reviewer -> documenter` — five distinct jobs in sequence, which is
"many jobs, many agents". Nothing in the codebase currently does homogeneous fan-out under the name
"swarm". So either the existing topologies get reclassified, or "swarm" means two different things
in the code and in the model.

### UI-105 — Ghost and Real are two separate chains, split by audience
**Severity: high (design) · Status: built (2026-08-28) · Source: Q-11, Q-12**

Built as specified — see UI-023 for what shipped. Two corrections to this finding's own analysis
surfaced during the build: the count is 2,748 blocks rather than 2,713, and the `counterparty` field
that looked like a ready-made discriminator is a constant placeholder in every existing transaction.

Q-14 is still open: `python/ledger/` (UI-027) remains in the tree.

**Original finding.**


> **Steven (Q-11):** *"The Ghost chain is only viewable locally. The Real chain is viewable by all on
> the chain. They are meant to be separate."*
>
> **Steven (Q-12):** *"If the TALA harness never goes online... the Real chain never gets used. If you
> connect with other users, use other skills, transact with AUTH or online storage, it gets recorded
> by the Real chain."*

**This overturns the approach suggested in UI-023.** That finding proposed adding a mode field so one
chain could carry tagged transactions. Steven wants two physically separate chains split by
**audience**, not one chain with a label.

The rule for what goes where is clean: **Ghost = local activity. Real = external interactions** —
peer contact, another party's skills, AUTH transactions, online storage. A fully offline install
never writes to the Real chain at all.

That confirms the existing 2,713 `node-create` transactions are all Ghost by definition: local
document activity with no external party involved. The chain in `blockchain/data/chain.json` is the
Ghost chain, correctly populated, and the Real chain simply does not exist yet.

**Primitives that already exist.** The Node chain server (`blockchain/src/app.js`, 1008 lines)
already serves `/chain/peers`, `/chain/peers/register`, `/chain/peers/heartbeat` and
**`/chain/reconcile`** — a peer directory and a reconciliation endpoint. So the shared-chain
machinery is partly present; what is missing is the second chain instance and the routing rule that
decides which one a transaction lands on.

**Q-14 resolved in the same pass.** `python/ledger/server.py` serves only `/chain/tx` and
`/chain/import` — a strict subset with no status, blocks, peers or reconcile — and no production
code imports it. Steven's instinct that "the other works better" is correct; the Python ledger is
safe to delete.

### UI-106 — Tala can read an HTML-NN but cannot act on one
**Severity: high · Status: FIXED 2026-08-23 · Source: Q-34**


> **Fixed.** The read/enumerate/write set gains the missing verb: `execute_node`, plus
> `list_executable_nodes` so a plan is not built around a node that will be refused.
> Two guards: only `workflow`/`flow` node types run, and only above 0.5 execution
> authority — every documentation and tool-catalogue node declares 0.0, and running one
> would turn a reference work into a program.
>
> The other half of Steven's sentence — *"pass one bit of info from one tool to the
> next"* — needed two more things. The executor had no `tool` node type at all, so a
> workflow could not call a registered tool; and node configs were static, so nothing
> could be passed between steps. Both added: `{{step_x.result.y}}` references resolve
> against accumulated state, and an unresolved reference **fails the step** rather than
> silently becoming an empty argument the tool then succeeds on.
>
> Verified end to end against real `compile_workflow_to_html` output, not a fixture:
> a two-tool flow ran from the node and the second tool received the first's output.

> **Steven:** *"A workflow is a series of tool calls, that pass one bit of info from one tool to the
> next until desired outcome. Tala has the ability to call an html-nn and act upon that. Or at least
> she should."*

The mechanism is **acting on a node**, not a dedicated `workflow.execute` tool and not a generic
RPC-invoke tool. That resolves UI-060's question without widening the agent's reach to every RPC in
the harness.

**Half of it is already built.** The registry has three node tools:

| Tool | Verb |
|---|---|
| `read_neural_node` | read |
| `list_neural_nodes` | enumerate |
| `update_neural_node` | write |

So she can **call** a node. What is absent is a verb for **acting on** one — there is no
`execute_node`, no `run_node`, nothing that takes a compiled workflow node and runs its steps.

That is the exact shape of UI-060: the compiled workflow node *documents* how to run itself
("invoke `workflow.execute` with Flow ID ..."), Tala can read that instruction, and she has no
mechanism to follow it. The gap is one verb wide.

### UI-107 — The tool registry is not HTML-NN backed
**Severity: high (mandate) · Status: FIXED 2026-08-23 · Source: Q-33**


> **Fixed.** `scripts/generate_tool_nodes.py` writes one `tool-*.html` per registered
> tool — 88 of them — carrying the prose retrieval matches on plus the real argument
> schema in node state. The Python registry remains what executes. Tool modules are
> discovered by scanning rather than a hardcoded list, which immediately turned up
> `sdk/agent/skills/`: those tools had no node and, registration being an import side
> effect, were invisible to the planner's catalogue too.

> **Steven:** *"The tool registry should be a collection of html-nn. If they are not we need to
> address that."*

**They are not.** Verified:

| | Count | What it is |
|---|---|---|
| `@register_tool` in `sdk/agent/tools.py` | **66** | Python functions — the actual registry |
| `nodes/sdk_agent_*.html` | **56** | **module** docs (`sdk_agent_planner`, `sdk_agent_shell`, ...) |

The counts do not correspond because the nodes document **modules, not tools**. There is no node per
tool, and no path from a node back to a callable.

This is a direct conflict with the HTML-NN universal-data mandate, and it is upstream of the
workflow palette (UI-059): a palette generated from HTML-NN cannot list tools until tools *are*
HTML-NN. Both questions collapse into this one.

### UI-108 — The vault is a deliberate exception to the HTML-NN mandate
**Severity: informational · Status: decided · Source: Q-18**

> **Steven:** *"This could be anything as it is being privately added and we want source of truth."*

Vault contents are **arbitrary files**, not HTML-NN. Recorded explicitly because the universal-data
mandate otherwise governs everything the harness creates or consumes, and a future reader would
reasonably assume it applies here too.

The reasoning is source-of-truth preservation: a privately added document is stored as it is rather
than transformed into a node. This is the one place the mandate is deliberately not applied.

**Interacts with Q-20/Q-65.** Steven also wants vault contents searchable from Unified Search. But
`vault:read` is refused unless network isolation is active (UI-102), while Unified Search's `web`
scope requires the network — so one search cannot currently span both.

### UI-109 — The environment buttons are presets, and each sets three things at once
**Severity: high (design) · Status: FIXED 2026-08-23 · Source: Q-25**


> **Built.** USB permits the embedded engine only — Steven's *"the embedded engine is all we
> have access to"*, and it doubles as a troubleshooting mode; LOCAL permits embedded and
> local; REMOTE permits any. Each also sets permitted storage.
>
> Enforced where the inference fallback chain is assembled, not at the panel — filtering at
> the panel would have left the restriction cosmetic, which is exactly what it was. Blocked
> providers are logged by name rather than silently dropped, and if a preset would leave no
> provider at all the embedded engine still answers.

> **Steven:** *"If you select the USB button you are running from a USB and all data stored is on
> USB. The embedded engine is all we have access to. Think of it as a troubleshooting mode. If you
> selected local then you are using local non-usb storage, inference engine such as ollama or
> lmstudio. And cloud means everything may be remote... It's also okay to think of it as presets."*

This substantially revises **UI-044**, which recorded the buttons as cosmetic chrome. They are
cosmetic *today* — but they were designed as presets, each setting **data root, permitted inference,
and permitted storage** together:

| Preset | Data root | Inference | Storage |
|---|---|---|---|
| **USB (ISOLATED)** | the USB itself | **embedded engine only** | USB only |
| **LOCAL (SYSTEM)** | local, non-USB | local engines — Ollama, LM Studio, llama.cpp | local |
| **REMOTE (CLOUD)** | may be remote | any, including cloud APIs | any, including cloud |

USB doubles as a **troubleshooting mode**: the narrowest configuration the system can run in.

**Three existing mechanisms already line up with this**, which makes it far less work than it looks:

- **Data root** — `resolve_user_data_dir()` already returns `TALA_USER_DATA || _application_root()`
  (UI-095). USB mode is that resolver pointed at the stick, which is the portable design already
  working today.
- **Inference restriction** — provider selection already filters by `type`, and lockdown (Q-62)
  already introduces a tier that forces local-only. USB is the same mechanism one notch tighter:
  `type == embedded` rather than `type in (local, embedded)`.
- **Isolation flag** — `isNetworkIsolated()` exists and gates `vault:read`, but is never set true
  (UI-102). USB mode is the obvious thing that should set it.

**Note the tier ordering**, since three separate concepts now restrict inference:

| Mode | Permitted inference |
|---|---|
| USB preset | embedded only |
| Lockdown (Q-62) | local + embedded |
| LOCAL preset | local + embedded |
| REMOTE preset | any |

Lockdown and the LOCAL preset land on the same restriction by different routes — worth deciding
whether lockdown simply *is* a forced LOCAL preset, or stays a separate orthogonal switch.

### UI-110 — Multi-database is content partitioning, not failover
**Severity: medium · Status: FIXED 2026-08-28 · Source: Q-42**


> **Closed, and not by building it.** This recorded that Steven wanted content partitioning
> — coding data in one database, conversation in another — and read that as the feature to
> implement. His later clarification makes clear that was an example of a *use*, not a
> specification: *"How you use them is up to the user not to the develouper."*
>
> Building partitioning into the harness would have replaced one imposed policy with another.
> The connections are provided and proven; what each one is for is the user's decision.

> **Steven:** *"The multi database is used for things like, I store all my info on coding on this DB
> and all my info on conversation on this DB."*

**The DATABASE panel describes the wrong feature.** Its stated rules are replication semantics —
*"Priority Order: DB connections are queried in the sequence defined"* and *"Automatic Failover: if
the primary replica database goes offline, operations automatically reroute to the next healthy
standby provider"*.

What Steven wants is **routing by subject domain**: coding data in one database, conversation data
in another. That is a different feature with a different data model — each provider needs a *content
scope*, not a priority rank, and there is no "standby" concept in it at all.

So UI-071 (providers never consumed) and UI-072 (failover architecturally impossible) both stand,
but UI-072's remedy is not "implement failover" — it is **replace the panel's stated contract**.
Priority ordering and the failover rule should come out, and a per-database content scope should go
in.

### UI-111 — Remote storage is optional and general-purpose
**Severity: medium · Status: fixed (2026-08-28) · Source: Q-41, corrected**

The panel describes storage as optional and general-purpose — text, images, audio, anything, with
remote HTML-NN as one common use — matching Steven's correction.

**A second claim on the same panel turned out to be false.** *"Bidirectional Sync — Tala propagates
changes to active storage providers in the background"* describes nothing that exists:
`sync_manager` records session events for `memory.py`, and nothing walks storage providers on a
timer. Replaced with what actually happens — storage is read and written on request.

**Original finding.**


> **Steven:** *"Not required. As well as not only. TALA can store text, image, audio or anything in
> the remote storage. A common use may be remote html-nn."*

> **Correction (same session).** This entry first read *"storage backends are for HTML-NN
> distribution, not backup"* and raised UI-068's priority on that basis. Both halves were wrong:
> remote storage is **not required**, and it is **not only** for HTML-NN.

Accurate statement: remote storage is an **optional, general-purpose** capability — text, images,
audio, anything — with remote HTML-NN as one common use among others.

UI-068 still stands as a real gap, but as a **capability that is unavailable** rather than a blocked
requirement: no client library exists for any of the nine offered backends, so nothing can be stored
remotely at all today. Q-40 confirmed the panel stays generic because the user supplies the backend;
that does not remove the need for the app to speak the protocol.

### UI-112 — Lockdown and the LOCAL preset are separate, and lockdown also tightens guardrails
**Severity: high (design) · Status: FIXED 2026-08-23 · Source: Q-25 follow-up**


> **Built, and kept orthogonal.** Lockdown restricts inference to embedded and local
> regardless of preset, and does **not** move the data root — moving a user's data is not a
> safety measure. It can land on the same restriction as the LOCAL preset by a different
> route, which is not a reason to merge them: the state reports `restricted_by` so the panel
> can say *why* a provider is unavailable rather than just greying it out.

> **Steven:** *"This should be a separate thing. Lockdown is a security override. This should
> probably include the strictest guardrail settings possible. Local is just the operational
> environment and these are the best settings for it."*

They stay orthogonal, and they are different *kinds* of thing:

| | LOCAL preset | Lockdown |
|---|---|---|
| kind | operational environment | **security override** |
| chosen because | running without cloud is the sensible default here | something is wrong, or the user wants maximum safety |
| data root | local, non-USB | unchanged |
| inference | local engines | forced local (Q-62) |
| network | normal | **cut** (Q-59) |
| guardrails | normal | **strictest available** — new |

**The guardrail tightening is new information.** Lockdown was previously scoped as a network cut
(Q-59) plus forced-local inference (Q-62). It also forces the guardrails category to its strictest
setting, which means it touches `guardrails_config` — a third subsystem beyond the network flag and
the provider chain.

So lockdown composes three existing mechanisms rather than needing a new one:

| Mechanism | State today |
|---|---|
| `isNetworkIsolated()` | exists, gates `vault:read`, **never set true** (UI-102) |
| provider chain filter by `type` | exists and works |
| `guardrails_config` category | exists and is genuinely enforced (D12) |

All three are live code. Lockdown is a coordinator over them, not new enforcement.

### UI-113 — Tala does not use git at all; the panel is the user's version control
**Severity: medium · Status: fixed (2026-08-28) · Source: Q-39, Q-66, corrected**

The git panel says whose version control it is: the user's own, for their code and content, and
*"Tala does not use git for herself"* — her updates come from the website, not a repository.

**Original finding.**


> **Steven:** *"The GIT panel should stay. But Tala does not use GIT for herself."*

> **Correction (same session).** This entry previously concluded that because user code projects
> come from git, *"clone plausibly is needed after all"* for Tala to work with user repos. That was
> my inference, not Steven's answer, and it is wrong. **Tala does not use git for herself in any
> capacity** — not for updates, and not to fetch repos to work on.

Accurate reading:

| Concern | Mechanism |
|---|---|
| Tala's own updates | **the website** |
| the user's code and content projects | **git** — the user's own version control, configured here |
| Tala's involvement with git | **none** |

The `.GIT` panel stays because it serves the user, not because Tala drives it. `system.trigger_update`
running a bare `git pull` to update Tala herself
([server.py:9993](../python/harness/server.py#L9993)) remains the wrong mechanism.

### UI-114 — Tala's original purpose — editing documents of any type — is barely represented
**Severity: high · Status: FIXED 2026-08-23 — with the remaining gaps named · Raised by Steven**

> **Fixed, and the most important part was not the feature.** Opening a PNG read it as UTF-8
> into the textarea and Save wrote the mangled text back: the file was destroyed by looking
> at it and pressing Save. Saving is now refused for images, binaries, and any file whose
> read failed -- an error message left in the buffer would otherwise be written over the
> file too.
>
> Files are classified text / image / binary from the extension, and an **unknown extension
> is treated as binary**: guessing wrong towards text corrupts files, guessing wrong towards
> binary only means the user has to say it is text. Images render from `encoding: base64`,
> which `file:read` already accepted, so carrying bytes safely needed no new IPC channel.
> Code is named by language on the badge.
>
> **What is still missing, stated rather than implied:** no syntax highlighting -- code is
> monospace text with a language label; images are read-only, with no crop, resize or
> annotate; and there is no file import into the Vault from here. All three are recorded on
> `ui-view-editor` so Tala's own map does not overstate what she can do.

> **Steven:** *"We technically forgot some things. Her original usage intent was a method to edit
> files and documents. Be them txt, images or whatever. This included code."*

This is a scope gap in the audit itself, not just in the code. Across 113 prior findings, **document
and file editing is almost absent** — because the surface that would hold it was never mapped.

**`view-editor` is one of the two views recorded in UI-012 as having no traced entry point**, and it
is where this purpose lives. Its entire markup is:

| Element | Purpose |
|---|---|
| `editor-textarea` | the editor |
| `editor-preview-pane` | rendered preview |

Wired at [renderer.js:182](../renderer/renderer.js#L182) — `btn-save-file`, `btn-editor-edit-mode`,
`btn-editor-preview-mode`.

**It is text-only.** The editor is a `<textarea>`, and preview is gated on file extension
([renderer.js:268](../renderer/renderer.js#L268)):

```js
const isPreviewable = tab.filePath && (tab.filePath.endsWith('.md') || tab.filePath.endsWith('.html'));
```

So against the stated intent — *"txt, images or whatever... this included code"*:

| Type | Supported |
|---|---|
| plain text | yes |
| Markdown, HTML | yes, with preview |
| **code** | as text only — no syntax highlighting, no language awareness |
| **images** | **no** — a `<textarea>` cannot hold one |
| anything else | no |

**Two things follow.** First, `view-editor` needs mapping properly — it was skipped because it has
no sidebar entry, and it turns out to be central rather than peripheral. Second, the gap between a
text-area and "edit files and documents, be them txt, images or whatever" is a feature question, not
a bug: it needs scoping before it can be planned.

Raised as **Q-67**.

---

### UI-115 — The file Explorer walks `.venv`, `.git` and `node_modules`, and never reaches user files
**Severity: critical · Status: FIXED 2026-08-23 · Raised by Steven, 2026-08-23**

> **Fixed.** listDir now skips node_modules, .git, .venv, .tala_snapshots, dist, __pycache__ and friends, and no longer stats every file. The full walk is 2,515 entries against a 10,000 cap, and nodes/, renderer/, sdk/, python/, docs/, electron/, scripts/ and tests/ are all reachable.

The Explorer drawer shows nothing usable. Cause traced end to end.

`FileExplorer.load()` ([renderer.js:549](../renderer/renderer.js#L549)) requests the **entire tree**:

```js
const result = await invoke('file:list', { dir: '.', recursive: true });
```

`listDir()` ([ipc-handlers.js](../electron/ipc-handlers.js)) has **no ignore list** — no
`node_modules`, no `.git`, no `.venv`, no dot-directory skip. It recurses to depth 20, capped at
`MAX_LIST_ENTRIES = 10_000`, and calls `fs.stat()` **per file** to record a size.

What that means at this repo root, in `readdir` order:

| Position | Entry | Files beneath |
|---|---|---|
| 5th | `.git` | 4,418 |
| 9th | `.tala_snapshots` | 2,197 |
| 10th | **`.venv`** | **37,718** |
| — | `node_modules` | 11,504 |
| — | `dist` | 896 |

`.git` and `.tala_snapshots` alone consume ~6,600 of the 10,000 budget. `.venv` exhausts the
remainder several times over.

**Every directory the user cares about sorts after those.** `docs/`, `electron/`, `nodes/`,
`python/`, `renderer/`, `sdk/` are all alphabetically later than `.venv` — so the budget is spent
before a single one is reached. Compounding it, ~10,000 individual `fs.stat()` calls make the walk
slow enough that the panel can sit on its loading state for a long time.

`this.expandedPaths.add('nodes')` at [renderer.js:515](../renderer/renderer.js#L515) shows the
intent — `nodes/` should be expanded on open. It can never be, because `nodes/` is never in the
result set.

**Fix shape:** an ignore list (`node_modules`, `.git`, `.venv`, `dist`, `.tala_snapshots`,
`__pycache__`), lazy per-directory expansion instead of `recursive: true` up front, and dropping the
per-file `stat` unless the size is actually displayed.

**Note the silent-failure pattern again.** `file:list` returns `{ ok: true, entries: [] }` when a
directory is missing — a success response for a non-result, so a caller cannot distinguish "empty"
from "not found". Same shape as UI-014 and UI-018.

### UI-116 — The editor is the centre pane, which resolves UI-012
**Severity: informational · Status: resolved · Source: Steven, 2026-08-23**

> **Steven:** *"The editing section was meant to be the center pane."*

This answers half of UI-012, which recorded `view-editor` and `view-logs` as views with no sidebar
entry and no traced entry point.

`view-editor` **has no sidebar entry by design**. It is not a destination — it is the centre pane
that the Explorer opens a file into. The intended flow is:

> `btn-toggle-drawer` opens the Explorer → click a file → it opens in the centre pane as a tab →
> `btn-editor-edit-mode` / `btn-editor-preview-mode` / `btn-save-file` operate on it

Which explains why those three buttons live in the **workspace tab strip** rather than in any view,
and why they are `display: none` until a file tab is active.

**The flow is broken at its first step**, not its last: UI-115 means the Explorer never lists a file
to click, so the editor is unreachable through its only intended route. That, combined with UI-114
(the editor is a `<textarea>` with `.md`/`.html` preview only), is why Tala's original purpose —
editing documents of any type — has almost no working surface today.

`view-logs` remains untraced; UI-012 stays open for that half.

### UI-117 — `view-logs` is a vestigial stub with no trigger
**Severity: low · Status: open · Closes the other half of UI-012**

The last unmapped view. Its entire markup:

```html
<section class="view" id="view-logs" aria-label="System Logs" hidden>
  <div class="view-header"><h1 class="view-title">System Logs</h1></div>
  <div class="empty-state">
    <p>System logs are directed to the bottom panel tabs.</p>
  </div>
</section>
```

A header and one sentence that points somewhere else.

It **is** registered as a TabManager destination — `'logs': 'view-logs'`
([renderer.js:127](../renderer/renderer.js#L127)) and `'logs': 'System Logs'`
([renderer.js:142](../renderer/renderer.js#L142)) — but **nothing ever opens it**. A search for
`openTab('logs')` across `renderer/` returns nothing. It is reachable only by calling TabManager
directly from the console.

Unlike `view-editor`, which is hidden from the sidebar *by design* because it is the centre pane
(UI-116), this one is simply left over. Logs already have two real homes:

| Surface | What it shows |
|---|---|
| bottom panel tabs | `TERMINAL 1`, `Blockchain Node`, `Python Harness` — live streams |
| `settings.logging` | the full event table, health matrix, filters and export |

So a third view adds nothing. Either delete it and its two map entries, or give it a purpose —
but as written it is a placeholder whose own text tells the reader to look elsewhere.

**UI-012 is now fully closed.** Both views that lacked a sidebar entry are explained: `view-editor`
by design, `view-logs` by neglect.

### UI-118 — Agents are principals with assigned privileges, and the schema has none
**Severity: high (design) · Status: FIXED 2026-08-28 · Source: Q-63**


> **Built.** `Principal(kind, id)` with `owner` / `agent` / `peer`. The owner is implicit and
> unrestricted and has **no rows** — the absence is the design, not an omission. An agent is
> assigned exactly like a peer, and there is no locally-initiated exemption: whoever asked,
> the agent is the actor and is checked.

> **Steven:** *"Ah so in short administrative privileges. For local actions by agent model. An
> agent, should be assigned just like a peer."*

This completes the access model in UI-101 and removes the special case I had assumed was needed.

**There is no "locally-initiated" exception.** The *agent* is always the actor, whoever asked. So
`AuthorizationGate` resolves against the acting agent's privileges in every case; when a peer
triggered the work, the peer's grant is consulted *as well*.

| Principal | Privileges | Assigned by |
|---|---|---|
| **Owner** | administrative — implicit, unrestricted | — |
| **Agent** | assigned, same structure as a peer's | the owner |
| **Peer** (contact) | assigned per resource, by invitation (Q-22) | the owner |

The owner's administrative standing is **the right to assign**, not a bypass — an action you
request still runs under the acting agent's privileges.

**Schema gap.** `agents` holds `id, name, type, role, crew_type, is_active, config`
([db.py:191](../python/harness/db.py#L191)) — **no privileges dimension**. Contacts at least have
`permissions` (nine booleans, itself insufficient per Q-22). Agents have nothing, so an agent
currently cannot be restricted at all.

**Both principal tables need the same grant structure**, which argues for one shared grants table
keyed by principal rather than bolting a second bespoke column onto `agents`:

> `(principal_type, principal_id) -> resource -> constraints`

with `principal_type in (agent, contact)`. That collapses cleanly to the *(contact, agent)* pair
from Q-36 when both are present, and to a single row when only the agent is.

**Relevant existing shape:** `AuthorizationGate.request_authorization(action_type, resource,
risk_class)` is already resource-scoped and already carries a risk tier — it needs a principal
argument, not a redesign.

### UI-119 — How Tala could operate the UI: CDP/Playwright vs the existing IPC path
**Severity: high (design) · Status: decision needed · Source: Steven's question, 2026-08-23**

> **Steven:** *"Electron is a chrome based node. Can we add playwright to it?"*

Technically yes. But there are two routes and they differ on security, not on capability.

**Route A — Playwright over CDP.** Playwright has two Electron modes:

| Mode | Fit |
|---|---|
| `_electron.launch()` | launches the app itself — wrong shape here, since Tala runs *inside* the app she would drive |
| `connectOverCDP()` | attaches to a running app via `--remote-debugging-port` — technically works |

The blocker is what that port is. **Chrome DevTools Protocol has no authentication.** Opening it
grants arbitrary JavaScript execution and full access to every window, to any process on the
machine that can reach the port. Electron does not expose one today — verified, no
`--remote-debugging-port` anywhere in `main.js` or `package.json`.

That sits directly against the model Steven specified across P1 and P2: lockdown as a network cut
(Q-59), per-agent assigned privileges (Q-63/UI-118), and a policy object gating what an agent may
reach (Q-35/36/37). A CDP port bypasses all of it — an agent restricted by policy could simply
drive the UI to do what it was denied.

**Route B — the existing IPC notification path.** Already proven end to end. `browser.navigate` is
broadcast from Python and handled in the renderer
([renderer.js:7147](../renderer/renderer.js#L7147)); that exact pattern works today.

And the seam is already clean. View switching is not scattered through the nav handler — it is one
call:

```js
TabManager.openTab(id, title, type, filePath = null)   // renderer.js:203
```

The sidebar buttons do nothing more than call it with a title from a lookup map. So `ui.navigate`
is a thin wrapper over a function that already exists.

**Comparison:**

| | Route A — CDP/Playwright | Route B — IPC |
|---|---|---|
| new dependency | Playwright for Electron | none |
| new attack surface | **an unauthenticated port with full app control** | none |
| respects UI-118 privileges | **no — bypasses them** | yes, calls route through the harness |
| element discovery / waits | strong, free | must be written |
| effort | moderate | small for navigate, more for click |
| second automation stack | yes, alongside UI-013's web Playwright | no |

**Recommendation: Route B.** Not because it is easier, but because Route A puts a hole through the
access-control model that P1 and P2 were spent designing. The capability gained — selector
discovery — is not worth an unauthenticated control channel into the app.

**Proposed shape**, three tools rather than one:

| Tool | Backing |
|---|---|
| `ui.navigate(view)` | broadcast → `TabManager.openTab` |
| `ui.describe(view)` | **read the `ui-view-*` node** — the map becomes the action vocabulary |
| `ui.click(element_id)` | broadcast → guarded click against an allowlist of known ids |

`ui.describe` is the part that closes the loop: the twelve nodes just authored already carry every
control id and its verdict, so Tala can answer "what can I do here" from her own memory rather than
from a live DOM scrape.

**If element discovery turns out to be genuinely necessary**, a narrower option exists than a global
CDP port: `webContents.executeJavaScript()` from the main process, reachable only over the existing
IPC channel and therefore still gateable by policy. That gives DOM reads without opening a port.

### UI-120 — `UI.switchView` is a dead call
**Severity: low · Status: open**

Found while tracing the navigation seam. [renderer.js:13417](../renderer/renderer.js#L13417):

```js
if (typeof UI !== 'undefined' && UI.switchView) {
    UI.switchView('settings');
}
```

**No `UI` object exists** — zero declarations of `const/let/var/window.UI` anywhere in
`renderer.js`. The `typeof` guard means it fails silently rather than throwing, so whatever action
was meant to jump the user to Settings simply does nothing.

Same family as UI-033, where `Manage Workers in Settings` targets a `data-tab` selector that does
not exist. Both are cross-view navigation attempts that silently no-op — which is the argument for
`ui.navigate` existing as one real function rather than being re-improvised at each call site.

### UI-124 — `POST /chain/reconcile` destroyed blocks, including the local Ghost chain
**Severity: critical · Status: fixed (2026-08-28)**

Filed the day before as a shared-chain hazard. It was worse than that: **there was a live caller
pointing it at the local Ghost chain**, and only the remote chain being down stopped it firing.

#### The endpoint

[blockchain/src/app.js](../blockchain/src/app.js) accepted any valid incoming chain of equal or
greater height and assigned it over the top:

```js
if (tempLedger.chain.length < talaLedger.chain.length) { /* 409 */ }
talaLedger.chain = tempLedger.chain;   // otherwise: overwrite
```

Longest-chain-wins whole-chain replacement. Sound between one machine and its own backup; data loss
on a chain more than one party writes to. Two peers who each append while the other is offline both
pass the height check, and whichever writes second discards the other's blocks — silently, since a
discarded block leaves no trace: the node simply holds a different chain than it did, and it
verifies. The transactions that vanish are by definition external ones, which are exactly what a
shared chain exists to record.

#### The caller

`ghost_persistence.pre_flight_reconciliation()` runs on **every goal execution**
([goal_engine.py:540](../sdk/agent/goal_engine.py#L540)) and from
[mcp_manager.py:181](../python/harness/mcp_manager.py#L181). When the local chain was shorter than
the remote one it fetched the remote blocks and POSTed them into the *local* node — replacing the
complete local history, the "everything this machine did" record, with a remote chain. Same shape as
the auto-rollback that destroyed the repo.

Three things kept it from happening. The remote at `talacore.app` was unreachable (502s and
timeouts throughout testing; `/chain/status` never once answered). `check_hash()` had to report
`major` first. And the local chain happened to be taller. None of those is a safeguard.

Worse, the fetch ran with `check_hostname = False` and `verify_mode = CERT_NONE`, so anything on the
path could answer as the remote chain — and what came back was chain data headed straight into the
local node.

#### The fix

**The endpoint merges instead of replacing.** Merging is at the transaction level, because a block
cannot be moved: its hash covers its own index and `previousHash`, and its signature covers the
hash, so re-siting somebody else's block invalidates their signature. Blocks both sides share are
kept exactly as they are; only the divergent tail is rebuilt.

| Outcome | When | What happens |
|---|---|---|
| `behind` | the incoming chain extends ours and we hold nothing it lacks | its blocks are appended untouched — hashes and signatures intact |
| `up-to-date` | it holds nothing we do not | nothing changes, including when it is *shorter* (previously a 409) |
| `merged` | both sides appended after the fork | both sets of transactions are unioned, ordered deterministically, re-mined from the fork point |

Re-mining changes the hashes of divergent blocks — unavoidable, since two blocks cannot occupy one
index — but no transaction is dropped, which is the property the overwrite lacked. A chain with a
different genesis is refused as a conflict: that is a different chain, not a fork, and merging it
would fabricate a history that never happened.

**Transaction identity is a fingerprint of the whole transaction.** Not the `id`, which is a
document identity — 2,748 local transactions carry 185 distinct ids, `settings-workflows` accounting
for 174. Not the `hash` either, which covers content and repeats on re-anchor (683 distinct across
the same 2,748). Nothing the chain assigns is unique.

**Deterministic ordering** by `(timestamp, fingerprint)` so two nodes merging the same pair converge
rather than diverging further with every reconcile. Tested by merging A-then-B and B-then-A and
comparing every block hash.

**The caller no longer writes to a chain at all.** `pre_flight_reconciliation()` now reconciles the
shared chain through `chains.reconcile()`, which pulls, verifies, and writes only Real rows. Its
premise was wrong as well as its mechanism: Ghost and Real are two chains split by audience, not two
copies of one chain, so Ghost is *expected* to be taller and the difference is the design rather
than a conflict.

**`check_hash()` asks a question that can be answered wrongly.** It compared the local chain's
height and hash against the remote's and called any difference `major` — which post-split is every
single run, blocking the task and triggering the sync that did the damage. It now reports `major`
only when the local chain is reachable and fails its own validity check.

**TLS verification restored**, and `talacore.app` is no longer hardcoded — a portable offline-first
install should not carry one external domain as an implicit dependency, least of all one whose
response is fed into the local chain.

#### Verification

12 tests in [`blockchain/test/merge.test.js`](../blockchain/test/merge.test.js) drive the **real
server over HTTP** against a scratch data directory, so the route, admin gate, chain verification
and merge all run as they actually do — including a tampered chain, a foreign genesis, an
idempotent re-merge, and the order-independence check. `node --test blockchain/test/merge.test.js`.
7 tests cover the Python side. Full Python suite green.

#### Key authentication (2026-08-28)

Closed in the same pass, and it uncovered a worse hole than the one it was meant to close.

**Any key was as good as any other.** `isValid()` verified each block against the `public_key` the
block itself carried, which proves only internal consistency: generate a key, sign with it, and the
block verifies. No bar at all on a chain other people write to.

**And a block could decline to name a signer.** The check read:

```js
if (current.signature && current.public_key) { /* verify */ }
```

A block carrying neither skipped verification entirely, so **forging a block was a matter of
deleting its signature**. This is the more serious of the two, and it makes the first one moot —
authenticating keys is pointless while a block can refuse to have one. Both are now required on
every block after genesis (genesis is unsigned on this chain by construction).

**The authority set** lives in `blockchain/data/authorities.json`, seeded on first run with this
node's own key and nothing else. That is not a placeholder default: on a chain whose purpose is
recording what happened between parties, who may write is exactly the decision that should not have
one. Managed through `GET`/`POST /chain/authorities` and `DELETE /chain/authorities/:fingerprint`,
all admin-gated — which parties a node trusts is not for an anonymous caller to survey. This node's
own key cannot be removed; without it the node would reject the blocks it signs itself.

The existing 2,748 blocks are all signed by one key — this node's — so seeding covers the whole
history. Verified: still valid with that signer permitted, refused with none.

**Withdrawing permission is not retroactive.** Blocks a former signer already wrote stay on the
chain; removing them would be the whole-chain rewrite this endpoint exists to avoid. It stops them
signing anything further.

**Both sides agree.** `chain_verify.verify()` takes an `authorities` set, and `fingerprint()` matches
`keyFingerprint()` in the Node server byte for byte, whitespace-normalised so a PEM that has been
through JSON or a text field still resolves to the same signer. A test asserts the two
implementations stay in step.

**An empty list refuses rather than permits.** `chains.reconcile()` will not import when no signers
are configured — importing from unknown signers because nobody answered the question is precisely
what the check exists to prevent — and it reports the fingerprints it saw, so the list can be filled
in. `chain.authorities`, `chain.set_authorities` and `chain.fingerprint` are the RPCs; the Blockchain
view manages them as chips and accepts either a pasted PEM or a fingerprint.

**A bug this caught in passing:** `chain.set_endpoint` wrote `{"endpoint": ...}` over the whole
`real_chain` settings row, dropping the signer list stored beside it — silently turning the check
off. Same shape as UI-056, one save overwriting a whole config object, which is the reason
`app_settings` exists at all. Found by an end-to-end run, not by the unit tests; now merges, and
regression-tested.

21 Node tests and 41 Python; full suite 1241 passed. The live chain is untouched.

#### Key distribution (2026-08-28)

The hard part is not fetching a key. It is knowing the key is theirs. There is no certificate
authority here, no web of trust, and no channel this system controls that an attacker in the middle
would not also control — so anything that fetches a key and admits it is not distribution, it is the
signature-trusting defect moved one layer out. Fetching and trusting are therefore separate acts,
and [`chain_keys.py`](../python/harness/chain_keys.py) never performs the second one.

| Step | What it establishes | What it does not |
|---|---|---|
| `GET /chain/identity` | the key a node says it signs with | that the node is who it claims |
| `POST /chain/prove` | whoever answers at that address holds the private key | who they are |
| fingerprint compared out of band | that the key belongs to the person you think | — |
| `chain.trust_key` | admission, as a deliberate act by the owner | — |

Proof of possession stops a directory advertising somebody else's key and having it admitted on
their behalf. It says nothing about identity, which no amount of network traffic can settle. The
fingerprint is what makes the out-of-band step usable: sixteen hex characters is short enough to
read down a phone line and long enough that nobody can produce a second key matching it.

**A trap in the obvious design, and why the prefix is not decoration.** A proof endpoint that signs
caller-supplied bytes with the block-signing key is a **forgery oracle**. Block signatures are made
over `block.hash` — a bare 64-character hex string — so an attacker builds a malicious block,
submits its hash as the "nonce", and walks away with a validly signed block from your node. The
signature is therefore made over `tala-proof-of-possession:v1:` + the nonce, which can never be a
bare block hash. A test does exactly this attack and confirms the result is unusable as a block
signature while remaining a valid proof.

The proof also checks that the nonce came back unchanged (a substituted one is a replay, which
proves nothing about who is answering now) and that the key signed with is the key advertised. The
advertised fingerprint is **recomputed, not believed** — a node that misreports its own would
otherwise get an out-of-band comparison to succeed against a key it will not sign with.

**Possession is evidence, not a gate.** A key handed over in person has no network proof and may be
perfectly trustworthy, so an unproven key can still be trusted; what matters is that the absence is
recorded rather than glossed over. Keys pasted directly are marked `possession not tested`.

`chain_key_offers` holds what has been obtained and what was decided. Refusing is kept rather than
deleted — a key that was offered and refused is worth still knowing about when it turns up again —
and withdrawing a trusted key is not retroactive: blocks it already signed stay on the chain.

`GET /chain/identity` is the one chain route deliberately **not** admin-gated. A public key is
public, and a key nobody can fetch cannot be distributed at all.

Verified end to end against a second chain node: published, fetched, challenged, recorded as
offered, its chain refused, trusted, its chain accepted, withdrawn, refused again. 26 Node tests and
25 Python.

#### Revocation (2026-08-28)

Only one kind of revocation can safely travel, and the distinction is the whole design.

A revocation **signed by the key it retires** is a fact anyone can check: nobody but the holder
could have produced it, so every node acts on it without needing to trust whoever relayed it. *"I no
longer trust your key"* is an opinion, and an opinion on a shared chain would let any signer evict
any other. So self-revocation goes on the chain as a `key-revocation` transaction and propagates;
local distrust stays local.

Propagation is not a notification. A node that reconciles reads the revocations off the chain and
acts on them — nobody is told anything, and there is nothing to deliver, retry or miss.

**Applied, not queued.** Waiting for someone to approve a revocation is exactly the wrong behaviour
for a key whose owner has declared it compromised, so `reconcile` acts on it in the same pass.

**A serious bug this uncovered, found by an end-to-end run against two real nodes rather than by the
unit tests.** Withdrawing a signer removed it from the authority list — and membership is consulted
for *every* block, historical ones included. So dropping a key refused everything it had ever
written: the revocation stopped it at block R, the removal refused it from block 1, and the chain
became permanently unverifiable. That is precisely the "retiring a key collapses its history"
failure the not-retroactive rule was written to prevent, arriving through the other door.

Withdrawal is therefore a **cutoff, not an erasure**. A signer stays recognised for what it signed
while trusted and may sign nothing after — whether it retired itself (cutoff at the revocation's own
block, which is what every other node will use) or was withdrawn here (cutoff at the current chain
height). The panel shows stopped signers rather than hiding them, since their history is still being
relied on. Three tests hold this down, including one asserting the broken behaviour stays broken so
nobody restores it by simplifying the cutoff away.

**Forging a revocation is checked for, in the ways it could be attempted.** A revocation signed by
anyone but the key it names is ignored — otherwise anyone could retire anyone's key, a denial of
service dressed up as a security feature. The fingerprint is derived from the carried key rather
than believed, so a revocation naming one key and carrying another retires nothing. The signature
covers the moment as well as the key, so a revocation cannot be lifted out and re-dated. And the
message is domain-separated (`tala-key-revocation:v1:`) for the same reason as proof of possession.

Retiring is irreversible, admin-gated, and requires repeating the fingerprint back — there is no
un-retire, because a retirement that can be withdrawn is not one. A retired key cannot be re-admitted
as a signer on any install that has seen the revocation.

Verified end to end across two real chain nodes: the peer retires its key, and this install learns
about it purely by reconciling — the key stops being active, keeps its history, cannot be
re-admitted, and its next block is refused. 36 Node tests and 29 Python; full suite 1295 passed.

#### Still open

Suppression, which is inherent rather than an oversight: a node that simply does not relay a
revocation keeps its own peers ignorant of it. Nothing here can fix that, and no gossip design can —
it is a reason to reconcile with more than one peer, not a bug to close.

Peers can advertise a `signing_key` in the directory, but nothing reads the directory to *suggest*
keys yet, so obtaining one still means having the peer's node URL.

---

### UI-121 — Scope: Tala fixing her own code and UI
**Severity: high (design) · Status: scoped, not built · Source: Steven, 2026-08-23**

> **Steven:** *"Tala needs to be able to fix her own code and her own UI."*

Scoping rather than building, per Steven's instruction. The headline: **most of this exists.** The
missing pieces are a JavaScript syntax gate, a way to apply a UI change without a restart, and a
rollback that is safe to trust.

#### What already works

| Capability | Mechanism | State |
|---|---|---|
| propose a change | `code.propose_patch`, `code.propose_file_write` | exists |
| apply a change | `code.apply_patch`, `code.write_files`, `write_workspace_file` | exists |
| write UI files | `ALLOWED_WRITE_SUFFIXES` includes `.js`, `.html`, `.json`, `.md`, `.py` ([safe_code_bridges.py:14](../sdk/agent/safe_code_bridges.py#L14)) | exists |
| write her own source | `_PROTECTED_WRITE_PATTERNS` blocks only 10 paths — the corpus scripts, astrology, `chat_mode.py`, persona and emotion nodes | `renderer/`, `sdk/`, `python/harness/` are **all writable** |
| verify Python | `test.run_validation` allowlist: `py_compile`, `pytest`, `uv run pytest` | exists |
| snapshot / restore | `RollbackRecovery.create_checkpoint()` / `restore_checkpoint()` | exists, see caution |
| size / path guards | 250 KB cap, no absolute paths, `BLOCKED_WRITE_PARTS` | exists |

**So permission is not the gap.** She can already edit `renderer/renderer.js`, `renderer/index.html`
and `sdk/agent/tools.py` today. The denylist protects her *identity* — persona, emotion, astrology —
not her code.

#### Gap 1 — JavaScript is written but never syntax-checked

`ALLOWED_VALIDATION_PREFIXES` ([safe_code_bridges.py:8](../sdk/agent/safe_code_bridges.py#L8)) is:

```python
("python", "-m", "py_compile"), ("python", "-m", "pytest"), ("uv", "run", "pytest"), ("pytest",)
```

**Python only.** A `.js` or `.html` write is permitted and then validated by nothing. Tala can write
a syntax error into `renderer.js` and the failure surfaces at the next window load, by which point
the UI is dead and the tool that broke it reported success.

*Fix:* add `("node", "--check")` to the allowlist and require it on any `.js` write. Cheap, and it
is exactly the check used on the `UIControl` edit in this session. For `.html`, parse-and-compare
rather than a linter.

#### Gap 2 — a UI change cannot be applied without a restart

No reload verb exists anywhere: `webContents.reload`, `location.reload`, `ui.reload` all return
nothing across `electron/`, `renderer/` and `python/`. So even a correct edit to `renderer.js` sits
inert until Steven restarts the app manually.

*Fix:* `ui.reload` over the same `python:notification` channel the new `ui_navigate` uses — the
transport is already proven. It should refuse while a task is mid-run, and should be a distinct
tool rather than an implicit side effect of writing a file.

#### Gap 3 — rollback exists but has a bad history

`RollbackRecovery` ([recovery.py:80](../sdk/agent/recovery.py#L80)) provides `create_checkpoint()`
and `restore_checkpoint()`. Its own docstring records a real failure: a snapshot that failed to
create was later "restored", *"printing a reassuring"* success message over a restore that never
happened.

This is the single most dangerous area in scope. This project has already lost 427 tracked files
plus everything gitignored to an automated rollback. **Any self-modification loop that can restore
automatically inherits that risk.**

*Recommended constraints, deliberately conservative:*

- **Never auto-restore.** A failed verification reverts *the files it wrote*, by content, not by
  restoring a directory tree.
- Prefer `git stash`/`git checkout -- <paths>` over `rmtree`-based snapshot restore for
  tracked files; the repo already is the checkpoint.
- `create_checkpoint()` returning falsy must abort the write, not proceed unprotected.
- Restore must verify afterwards and report what it actually changed.

#### Proposed loop

> propose → **verify syntax** → write → **run the relevant tests** → reload if UI → report
> — and on any failure, revert *only the written files* and say so.

#### Sizing

| Piece | Effort | Risk |
|---|---|---|
| add `node --check` to the validation allowlist | small | low |
| require syntax gate on `.js` / `.html` writes | small | low |
| `ui.reload` tool + renderer handler | small | low |
| targeted revert-written-files on failure | medium | **high — see Gap 3** |
| wire the loop together | medium | medium |

**Suggested order:** Gap 1 first — it is small, it is pure gain, and it makes every later step
safer. Gap 2 next, since it makes UI edits actually testable by Tala herself. Gap 3 last and
carefully, because it is where this project has been burned before.

#### Open question

**Q-68 — should Tala's self-edits require approval before they land?** She can write to
`renderer/` and `sdk/` unsupervised today. The propose/apply split (`code.propose_patch` then
`code.apply_patch`) suggests review was intended, but nothing forces the propose step. Under the
UI-118 privilege model this becomes a per-agent grant: an agent may be permitted to *propose* but
not to *apply* to its own source.

---

## B. Self-navigation infrastructure gaps

### UI-122 — `run_workflow_node` mocks script execution and reports completion
**Severity: high · Status: FIXED 2026-08-23 · Found while fixing UI-060**

> **Fixed by deleting the second implementation.** The per-node dispatch is extracted to
> `WorkflowExecutor.run_single_node`, and the interactive driver calls it instead of
> imitating it. Two copies would drift, and the copy that drifted would be the one quietly
> doing nothing. A failed step now follows `onFailure`, or stops with `*** WORKFLOW
> FAILED ***` naming the step and the reason — it can no longer report completion after a
> step that did not run.

`run_workflow_node` is a separate, interactive workflow driver — it walks a flow asking the
user each `interactive_prompt` question — and it reads its flow from the HTML-NN, which is
right. But its `custom_script` branch does this:

```python
elif node_type == "custom_script":
    # Mock execution or basic handling
    state["variables"][current_id] = "Executed script"
```

Nothing runs. The walk then continues to the `output` node and returns
`*** WORKFLOW COMPLETE ***` with "Executed script" recorded as the step's result. A flow with
a script step reports success having executed nothing — the same silent-success shape as
UI-014 and UI-018.

Not fixed here because the two tools serve different purposes and merging them is a design
decision, not a repair: `run_workflow_node` is a conversational interview driver,
`execute_node` runs actions. The likely resolution is that `run_workflow_node` delegates
non-interactive steps to `WorkflowExecutor` rather than imitating them.

### UI-123 — The system has no machine-readable declaration of its own architecture
**Severity: high (methodological) · Status: FIXED 2026-08-23 · Raised by Steven**

> **Steven:** *"you created your prob under default understandings of how systems work. But I am
> building a diffrent type of system what needs to happen is systems need to be able to loook at
> mine and determine its default system. Otherwise Ill run into the same problem with other
> remote LLM"*

Raised after a health probe written during UI-091 measured the wrong thing. Asked to report on
"memory", it was written against the ordinary assumption that memory means a database service: it
counted registrations on `MemoryManager` and reported `HEALTHY`, while never looking at the
ChromaDB collection that actually holds the nodes. Nothing errored. The probe was wrong in a way
that read as correct.

Prose describing the architecture already existed — `system-tala-html-nn-memory-runtime` and
`system-tala-agent-architecture`, both accurate — and did not prevent it, because prose answers a
reader who has already decided to go and look. The generalisation Steven drew is the important
part: **the next model will make the same mistake**, and a remote one will make it without anyone
watching.

> **Fixed.** `system-architecture-contract`, an HTML-NN generated by inspecting the running system
> rather than hand-written, so it cannot drift into a confident description of something no longer
> true. It states what the terms mean *here*, and — the part prose was missing — enumerates the
> specific default assumptions that fail, each with the consequence of assuming it anyway:
> memory-is-a-database, database-is-source-of-truth, data-lives-in-an-OS-profile-directory,
> configured-means-working, empty-means-nothing-matched.
>
> Discoverable three ways, because being findable was never the problem: an orientation line in
> the system prompt (before the user line — knowing who you are talking to is no use if you are
> wrong about what you are talking through), the `describe_architecture` tool, and the
> `system.architecture_contract` RPC for an external caller that is not an agent. It also tells
> the reader not to trust it and names the calls that report measured state instead.

### UI-007 — Tala has no tool that acts on the UI
**Severity: high · Status: CLOSED 2026-08-23**

> **Built.** `ui_describe`, `ui_navigate` and `ui_click` in `sdk/agent/ui_control.py`, backed by the
> `ui-view-*` node map and routed over `python:notification`. See UI-119 for why not CDP.

There is no `ui.navigate` / `ui.describe` tool. The only UI-touching tool is
`docs.refresh_node_grid` ([tools.py:2084](../sdk/agent/tools.py#L2084)), which is a refresh
*hint* and cannot change views.

Transport already exists: `python:notification` is a live Python → renderer push channel,
handled in seven places in `renderer.js` and emitted from
[main.js:474](../electron/main.js#L474). A navigation tool can ride it.

Without this, a UI map only lets Tala *describe* the interface, never operate it.

### UI-008 — The only UI documentation node is generic boilerplate
**Severity: high · Status: FIXED 2026-08-23**

> **Fixed.** Eleven `ui-view-*` nodes plus `ui-map-index`, generated by
> `scripts/generate_ui_nodes.py`. Not boilerplate: each carries the view's purpose,
> how it is reached, its controls, Steven's own description of what the screen is for
> (`operator_label`), and the open findings against it. `tests/test_ui_map_drift.py`
> (25 tests) asserts every documented element id exists in `index.html`.

[nodes/program-doc-renderer-ui.html](../nodes/program-doc-renderer-ui.html) is 108 lines of
prose that names **zero** views and **zero** controls, against a surface of 700 unique element
ids and 11 views. Tala cannot navigate from it.

### UI-009 — The doc generator will overwrite hand-authored UI content
**Severity: high · Status: FIXED 2026-08-23 · Blocked UI-008**

> **Fixed by namespace.** The UI nodes live under `ui-*`, and the SDK generator only
> ever writes `sdk_agent_<module>.html` (`generate_sdk_agent_nodes.py:153`), so it
> cannot collide with or flatten hand-authored UI content.

`program-doc-renderer-ui.html` is fingerprint-generated by `self_documenter` from
`renderer/index.html;renderer/renderer.js;renderer/styles.css`. Hand-authoring rich content at
that `doc_target` means the next documentation regeneration flattens it back to boilerplate.

Needs a separate node-id namespace the generator does not own — proposed `ui-view-*` plus a
`ui-map-index` — or an explicit generator exemption.

### UI-010 — No retrieval wiring for UI nodes
**Severity: high · Status: FIXED 2026-08-23 — verified live**

> **Fixed and now actually checked.** The live check I said this needed: the ChromaDB node
> collection went 127 -> 225 on indexing, and the families present are `tool` (90),
> `program` (14), `system` (14), `settings` (13), `ui` (12). The UI map nodes are indexed
> and retrievable. `retrieve_relevant_nodes()` calls `index_nodes()` on every query, so a
> newly written node is retrievable on the next search without a rebuild step.
>
> Worth recording, because it is the opposite of what the Postgres side shows: `memory_index`
> holds 2,502 rows and **zero** of source_type `node`/`neural-node`, though its ranking SQL
> boosts exactly those. The two engines cover different things — Chroma is the HTML-NN
> memory, Postgres is operational memory — and conflating them is what UI-123 is about.

> **Partly done.** The nodes are authored with `memory_authority` 0.7 (views) and 0.85
> (index), so they are recallable rather than mute. Whether a navigation question
> actually ranks them first can only be confirmed against the running harness —
> `MemoryManager.search` returns empty with no providers registered offline — so this
> stays open until verified live rather than being marked on an assumption.

700 ids will not fit in a prompt. Authoring the nodes is not sufficient; "where do I change
the model?" must actually pull `ui-view-settings` into context. Without this the work lands
as another dead subsystem: built, never invoked.

### UI-011 — No drift guard
**Severity: medium · Status: CLOSED 2026-08-23**

> **Built.** `tests/test_ui_map_drift.py` — 25 tests asserting every documented element id
> exists in `renderer/index.html`, every `view_id` names a real section, `ui-map-index` matches
> the nodes on disk, and ids are literal rather than shorthand. Confirmed to fail on a
> deliberately renamed id.

Nothing asserts that documented element ids still exist in `index.html`. The map will rot
silently as the UI changes. Needs a test, or a source fingerprint on each `ui-view-*` node.

### UI-012 — `view-editor` and `view-logs` have no traced entry point
**Severity: low · Status: CLOSED 2026-08-23**

> **Resolved.** Both explained, for different reasons. `view-editor` has no sidebar entry **by
> design** — it is the centre pane the Explorer opens files into (UI-116). `view-logs` is
> **vestigial** — registered as a TabManager destination but never opened by anything (UI-117).

11 views exist but only 9 appear in the sidebar rail. How the editor and logs views are
reached has not been established. Must be resolved before the map claims to be complete.

---

## C. Open questions for Steven

Code cannot answer these; they need Steven's intent, and his wording is what Tala should
repeat back to a user.

| # | Question | Blocks |
|---|---|---|
| Q-1 | Which behaviour for the status-bar token field — wallet balance, or usage meter? | UI-001, UI-002, UI-003 |
| Q-2 | The green dot beside **AGENT CHAT** — model connected, harness alive, or something else? | landing node |
| Q-3 | `MOOD` / `MD` / `JSON` — wire them up, or delete them? | UI-004, UI-005, UI-006 |

| Q-5 | `Exec:` / `Mem:` card chips — day-to-day meaning in Steven's words, not just the attribute names? | landing node |
| Q-6 | Should Tala's browser tools be **re-pointed** at the visible webview, or should Playwright stay and the webview merely **mirror** what it does? | UI-013, UI-016 |
| Q-7 | Is watching Tala browse a requirement, or is a headless browser fine so long as she reports back? This decides whether UI-013 is a defect or a design choice. | UI-013 |
| Q-11 | Ghost vs Real: should they be **one chain with tagged transactions**, or **two separate chains** that reconcile when online? UI-023 is a schema change either way, but the shape differs completely. | UI-023, UI-024 |
| Q-12 | What actually goes on the Real Chain — everything mirrored, or only value-bearing transactions? Currently 100% of traffic is `node-create`, which sounds Ghost-only by Steven's definition ("non-value... tracking locally"). | UI-024 |
| Q-13 | Is `https://talacore.app` (already allow-listed in the Electron CSP) the intended Real Chain host? | UI-025 |
| Q-14 | `python/ledger/` vs `blockchain/src/app.js` — delete the Python one, or promote it to be the Real Chain? | UI-027 |
| Q-15 | The test fixtures already on the live Task Board (and in `nodes/`) — purge them, or keep as sample data until real tasks exist? | UI-028 |
| Q-53 | The nine hardcoded subsystem statuses — wire each to a real probe, or reduce the panel to the three that are real? A permanently-`DEGRADED` banner trains the user to ignore it. | UI-091 |
| Q-54 | Is `mem0` intended to be a real dependency (promote out of optional extras and install it) or should the "Dual Memory" capability drop the mem0 half? | UI-093 |
| Q-51 | Is the architecture diagram meant to be **aspirational** (what Tala will be) or **descriptive** (what runs today)? Four of nine boxes are only true aspirationally. A proposal can use either, but not silently mix them. | UI-088 |
| Q-52 | Should the diagram gain a **Filesystem / HTML-NN Node Store** box? It is the project's defining component and is currently absent, with its role misattributed to PostgreSQL. | UI-089 |
| Q-49 | Should `logger.*` calls be bridged into `agent_log` (a logging handler that writes through `log_to_db`), or should the 253 call sites be converted individually? The handler approach captures everything including future call sites. | UI-085 |
| Q-50 | Which log surface should Tala get as a tool — read-only `get_events` + `get_health`, or also `archive`/`clear`? Self-diagnosis needs only the first two; the latter two let her erase her own audit trail. | UI-086 |
| Q-48 | **Keep messaging channels at all?** Steven is undecided on a proprietary comms path. Keeping it means enforcing the allowlist (UI-083) and either building a Discord adapter or removing Discord from the dropdown (UI-084). Dropping it means deleting `channels.py`, the `chat_channels` table, and this panel. Nothing else in the audit depends on the answer. | UI-083, UI-084 |
| Q-46 | Should nodes carry a `clearance` value in their `node-state` block? Without it the firewall slider is decorative. Which nodes would be above 0 — PII, vault, identity? | UI-082 |
| Q-47 | `gates.py` uses credential **name + priority=1** as an authorization allowlist. Is that intended? Renaming or reordering a credential in the AUTH panel silently changes what the agent may do. | UI-081 |
| Q-43 | Should `settings.runtime` be **removed in favour of Contacts**, become a *view* onto contacts that have granted `nodes` permission, or stay as an anonymous-endpoint option alongside identity-based peers? | UI-075, UI-076 |
| Q-42 | Is multi-database routing / read-replica failover actually wanted, or is `settings.DB_URL` the intended single source and this panel should reflect it read-only? | UI-071, UI-072 |
| Q-40 | Which storage backends are actually needed first? Nine types are offered and none are implemented; S3-compatible alone would cover AWS, MinIO, Backblaze, Spaces and iDrive e2 in one client. | UI-068 |
| Q-41 | Is off-machine backup of `nodes/` a real requirement? If so it interacts directly with UI-056 — the storage panel currently *implies* a safety net that does not exist. | UI-069, UI-056 |
| Q-39 | Do clone/push operations need building, or is Tala only ever meant to `pull` its own repo? The panel promises all three; only a provider-blind `pull` exists. | UI-067 |
| Q-38 | Should search providers get a real health probe (a cheap authenticated request), or should the badge simply be removed rather than assert a state nothing checks? | UI-064 |
| Q-35 | Should guardrails **absorb** contacts-permissions and firewall into one policy object, or stay content-only with a new "Access Policy" surface alongside? Steven's framing says absorb. | UI-061, UI-062 |
| Q-36 | What is the subject of a policy — a contact, an agent, or a (contact, agent) pair? The example implies the pair, which is the most expressive and the most work. | UI-062 |
| Q-37 | "A certain port" — is that restricting an outbound call Tala makes, or an inbound port a peer may reach? They are opposite enforcement points. | UI-063 |
| Q-33 | Should the palette be generated from the **tool registry** (66 tools), from **HTML-NN nodes**, or both? Steven's framing says HTML-NN; tools are what actually execute. | UI-059 |
| Q-34 | To let the agent run workflows: register a `workflow.execute` **tool** wrapping the RPC, or give it a generic RPC-invoke tool? The latter is far broader and worth deciding deliberately. | UI-060 |
| Q-31 | Should the profile be injected into every chat system prompt, or retrieved on demand? Always-inject costs tokens each turn; retrieval means it is sometimes absent. | UI-057 |
| Q-32 | Privacy visibility vs Contacts permissions are two access-control surfaces with different keys. Should the six dropdowns drive the per-contact permissions, or be removed in favour of Contacts? | UI-058, UI-040 |
| Q-28 | **Are crews a separate orchestration concept, or a friendlier front-end onto swarm topologies?** Topologies already do hierarchy, distinct functions, and execution, and `TopologyNode.agent_id` already binds a node to a persona. Building crew hierarchy separately duplicates a working engine. This decides UI-049 and UI-050. | UI-049, UI-050, UI-051 |
| Q-29 | If crews stay separate: does a boss *route work to* subordinates, or merely *own* them organisationally? Routing means reusing the swarm executor; ownership is just a tree. | UI-049 |
| Q-24 | `APPLY CHANGES` — should settings become staged-then-applied, or should the button be removed because panels already save on change? | UI-045 |
| Q-25 | `USB (ISOLATED)` / `LOCAL` / `REMOTE` — what should each actually *do*? It reads as a security control and currently does nothing. Should it drive `isNetworkIsolated()`? | UI-044, UI-039 |
| Q-26 | Is `GLOBAL` vs `WORKSPACE` scope a real intended feature, or leftover chrome to delete? | UI-044 |
| Q-27 | Should embedding-only providers be hidden from the inference priority list, shown greyed as "not in chat chain", or moved to their own section? | UI-048 |
| Q-21 | Should `p2p.marketplace_search` require an identified, permitted caller? Right now it is the one p2p path with no identity parameter at all. Decides the shape of UI-040. | UI-040 |
| Q-22 | On enrollment, should a peer's `requested_permissions` be treated as a **request awaiting local approval** rather than applied directly? That would make the checkboxes authoritative. | UI-041 |
| Q-23 | Should skill sharing be per-skill rather than one global `skills` toggle — e.g. a `shared: true` flag in each `skills/*.yaml`? | UI-040 |
| Q-18 | Vault scope — plain files on disk, or HTML-NN documents like everything else? The universal-data mandate suggests the latter, but "editable private documents" sounds like arbitrary files. Decides the whole build. | UI-036, UI-037 |
| Q-19 | "Local LLM could reference" — does that mean Ollama-only? The isolation guard refuses vault reads whenever network is active, so a remote provider could never read it. | UI-038 |
| Q-20 | Should the vault be searchable from the Research view's Unified Search, as a source alongside Web / Chat Sessions / Neural Nodes / Tasks? | UI-038 |
| Q-17 | What retention do you want for finished tasks — auto-hide `complete`/`blocked` after N days, a "show completed" toggle, or archive-on-done? Decides UI-034. | UI-034 |
| Q-16 | Is there a real (non-test) task workflow in use yet? If the board has only ever held fixtures, UI-030's 50-row cap and UI-031's payload risk have never been exercised in anger. | UI-030, UI-031 |

### Answered

| # | Question | Steven's answer (2026-08-23) |
|---|---|---|
| Q-8 | Grounded Q&A over selected sources — in scope for the research view, or does chat cover it? | **Chat covers it.** But there is no way to flag a research result *into* chat — logged as UI-022. |
| Q-9 | Should a failed search provider fall back to the crawler silently? | **No. A failed provider must be visible.** Fixes UI-018 and UI-020 in the same direction. |
| Q-10 | Is the narrower Agent Chat panel a layout bug? | **No** — deliberate resize by Steven. Not a defect; closed. |
| Q-45 | Has a rollback already reverted working-tree UI? | **No.** The checkout was 8 commits behind `origin/main`; `Scan for servers` came from `6e78a02`. UI-079 retracted. |
| Q-44 + Q-30 | Which directory does the running app use, and is `TALA_USER_DATA` set? | **The install directory, as intended. `TALA_USER_DATA` is unset.** Steven's intent — Tala self-sustaining from her own directory, USB-portable — is implemented and working. Diagnostic in UI-095. |
| Q-4 | `Connect VPS` — connects to what, and what changes vs `Local Only`? | **Answered by code, not by Steven.** It drives `chain.connect_vps`, switching the chain connection off local. Per README, `Stay Local` means all interactions use ghost tokens and never touch the blockchain. Still worth Steven confirming the intended user-facing wording. |

---

## D. Verified working — landing page

Recorded so these are not re-checked on later passes.

**Left icon rail** — [index.html:47-140](../renderer/index.html#L47), handler at
[renderer.js:132](../renderer/renderer.js#L132). All 11 buttons match the screenshot in order:

| Icon | id / `data-view` | `title=` |
|---|---|---|
| hamburger | `btn-toggle-drawer` | Toggle File Explorer |
| globe | `browser` | Web Browser |
| book | `research` | Research Library |
| neural (lit) | `neural-node` | Neural Nodes — default active |
| chain | `blockchain` | Blockchain Ledger |
| arrows | `agent-builder` | Agent Builder |
| circle | `mission-control` | Mission Control |
| person | `vault` | Vault Directory |
| people | `contacts` | Contacts Directory |
| power | `btn-app-quit` | Shutdown App |
| gear | `settings` | System Settings |

**Other confirmed-live controls**

- `#global-command-bar` (Ctrl+P) — [renderer.js:7216](../renderer/renderer.js#L7216)
- Workspace tab strip — genuinely dynamic, tabs built at [renderer.js:477](../renderer/renderer.js#L477)
- Grid / Graph toggle — `switchView` at [renderer.js:7991](../renderer/renderer.js#L7991)
- Chat modes `RP` / `Hybrid` / `Assistant` — real RPC to `agent.set_chat_mode`,
  [server.py:4659](../python/harness/server.py#L4659), backed by `python/harness/chat_mode.py`
- `NEW` chat — [renderer.js:4807](../renderer/renderer.js#L4807)
- `TTS` — persisted toggle
- `Connect VPS` — [renderer.js:13242](../renderer/renderer.js#L13242) (behaviour still to be
  described, Q-4)
- Bottom dock icons — Clear Console, Kill Command Process (disabled when idle), Toggle
  Terminal Size
- Chat input icons — Attach image, Capture screen for vision, Voice input (5s, local Whisper)

**Not a control:** the `CONSOLE` pill is a CSS `::before` brand label,
[styles.css:800](../renderer/styles.css#L800).

**Card chips** — `Exec:` / `Mem:` render `data-execution-authority` / `data-memory-authority`;
`Trust:` renders `data-trust-tier`; the badge renders `data-node-type`.

---

## D2. Verified working — Web Browser view

All **human-facing** controls work. The `WebBrowser` module is at
[renderer.js:7055](../renderer/renderer.js#L7055).

| Control | id | Behaviour |
|---|---|---|
| ◀ | `btn-browser-back` | `webview.goBack()` |
| ▶ | `btn-browser-forward` | `webview.goForward()` |
| 🔄 | `btn-browser-refresh` | `webview.reload()` |
| address bar | `browser-url-input` | Enter → `WebBrowser.navigate()` |
| viewport | `browser-iframe` | Electron `<webview>`, default `https://duckduckgo.com/` |

- The address bar self-updates from the page via `did-navigate` and `did-navigate-in-page`.
- A `will-navigate` hook rewrites `file://` URLs containing `/nodes/nodes/` down to `/nodes/` —
  a workaround for a relative-path resolution bug in node links.
- `browser.navigate` push from Python works end-to-end (listener at
  [renderer.js:7147](../renderer/renderer.js#L7147)).
- Playwright dependency chain is intact — package in `.venv`, browser binaries downloaded. No
  drift.

The element id `browser-iframe` is a leftover name; the element is a `<webview>`, not an
`<iframe>`. The code comments acknowledge this. Worth noting so the eventual node does not
teach Tala the wrong element type.

---

## D3. Verified working — Research Library

`ResearchManager` is at [renderer.js:3731](../renderer/renderer.js#L3731).

| Control | id | Behaviour |
|---|---|---|
| Enter Query | `research-subject` | Enter key or button triggers `search()` |
| Search Engine | `research-search-engine` | **populated at runtime** from `search.get_providers`; a `locked` provider auto-selects |
| All / Web / Chat Sessions / Neural Nodes / Tasks | `toggle-source-all`, `.source-toggle` | mutually exclusive — checking `All` clears the rest and vice versa |
| Search | `btn-research-search` | → `research.unified_search` |
| result rows | `research-sources-list` | click selects → `memory.browse` loads full context; checkbox multi-selects |
| Save / Distill | `btn-research-save-node` | → `research.distill_to_node` → `file:write` into `nodes/` |
| Context Viewer | `research-preview-area` | renders `fullContext` of the selected result |
| trust badge | `context-viewer-badge` | hidden until a result is selected |

Confirmed end-to-end:

- **The named key feature works at every layer** — user-added providers reach the dropdown, the
  selection reaches `SearchManager`, and a specific `provider_id` filters to that provider
  ([search_manager.py:135](../python/harness/search_manager.py#L135)).
- The `crawler` sentinel value is handled correctly as a deliberate direct-crawl choice
  ([search_manager.py:131](../python/harness/search_manager.py#L131)).
- `locked` providers are respected when no explicit choice is made.
- Memory and web are searched in parallel scopes and results merged, sorted by score
  ([memory.py:553](../python/harness/memory.py#L553)).
- **Prompt-injection scanning on every snippet** before display, via `handle_memory_scan_context`
  — flagged snippets are replaced with sanitised text. Good defensive work; worth teaching Tala
  that this exists.
- Per-source errors have a render path (red banner) — the mechanism is sound, it is only
  starved of input (UI-020).
- `research.distill_to_node` is real, registered, and has test coverage in
  `tests/test_memory.py:132`.

---


## D4. Verified working — Blockchain Ledger

`BlockchainViewer` is at [renderer.js:6422](../renderer/renderer.js#L6422). Everything present
on this screen works; the findings above are all about what is *absent*.

| Control | id | Behaviour |
|---|---|---|
| refresh | `btn-refresh-blockchain` | re-runs `load()` |
| Height | `chain-height` | live from `/chain/status` → `${status.height} blocks` |
| Validity | `chain-validity` | live — `VALID` / `COMPROMISED` from `status.isValid`, with matching glow class |
| RPC Endpoint | — | **static text**, see UI-025 |
| chain flow | `blockchain-chain-flow` | one card per block, arrow connectors between |

- Block cards render index, nonce, short hash, short previousHash, local time, and a transaction
  list with creator and state hash. Hash cells carry the full hash in a `title` tooltip.
- Genesis is special-cased: `Genesis Block (No transactions)` when the tx list is empty.
- The offline path is genuinely good — an unreachable node produces a clear error state, sets
  Height to `0 (Offline)`, and flips Validity to `UNREACHABLE` with the invalid glow. No silent
  success here, in contrast with several other findings in this document.
- The chain itself is substantive and real: 2,714 blocks, AES-256-GCM encrypted documents,
  PoA signatures and public keys on every block, genesis mint of 440,000,000,000,000.

Creators seen on-chain, useful for the eventual node: `tala-system` (1,638), `tala-agent-loop`
(694), `tala-goal-engine` (261), `tala-function-documenter` (90), `tala-feedback-adjuster` (15),
`tala-feedback-auditor` (15), `system` (1).

---


## D5. Verified working — Agent Builder

`AgentBuilder` tab switching at [renderer.js:12039](../renderer/renderer.js#L12039).

**Workers** — `builder-workers-list` renders profiles from the agents table; the reference
screenshot shows `Tala RP (tala-rp)`, `Role: Standalone`, `Active`, matching the status-bar agent.

**Topologies** — the strongest sub-tab.

| Control | id | Behaviour |
|---|---|---|
| ＋ New | `btn-topology-new` | opens the editor blank |
| topology list | `topology-list` | loaded from HTML-NN nodes — `swarm-topology-code-repair`, `swarm-topology-parallel-audit`, both labelled `neural node` in the UI |
| ✎ Edit / 🗑 Delete / ▶ Run Swarm | `btn-topology-edit` / `-delete` / `-start` | hidden until a topology is selected |
| graph canvas | `topology-graph`, `topology-edges-svg`, `topology-nodes-container` | renders agent nodes plus SVG edges with arrowheads and conditional edge labels |
| editor | `topo-edit-id`, `-name`, `-prose`, `topo-edit-nodes`, `topo-edit-edges` | add/remove agents and edges, save or cancel |
| editor errors | `topology-editor-error` | dedicated inline error panel |

Confirms the HTML-NN mandate is honoured here: topologies are authored *as neural nodes*, and the
UI labels them as such. The `Code Repair Swarm` renders correctly as
`planner → coder → tester → reviewer → documenter`.

**Task Board** — seven columns wired to `kanban-pending|planned|ready|running|review|blocked|done`,
fed by `task.list`. Cards show short id, description, assigned worker, and a worktree badge with
branch name when one exists. `+ Worktree` calls `task.create_worktree` and **does** surface errors
via alert; `Assign` calls `task.assign_worker`. Both refresh the board on success. Unknown statuses
fall back to the Inbox column rather than vanishing.

**Runs** — `monitorSwarmRun(taskId, topologyId)` is real; the empty state correctly instructs the
user to click a Swarm Root Task. The blue-bordered root card and tooltip work as designed.

---


## D6. Verified working — Mission Control

`MissionControl` at [renderer.js:10060](../renderer/renderer.js#L10060). Polls every 4s while the
tab is active.

| Control | id | Behaviour |
|---|---|---|
| Refresh | `btn-mc-refresh` | `refreshAll()` — tasks, telemetry, git status, schedules |
| HALT LOOP | `btn-mc-halt` | halts the goal loop |
| New Task | `btn-mc-task-new` | opens the task editor |
| Clear Queue | `btn-mc-task-clear` | `task.clear`, **with an accurate confirm naming the row count**, explicitly stating it cannot be undone |
| Edit / Delete | delegated on `_taskList` | `task.update` / `task.delete`; delegated because the 4s poll re-renders rows continuously |
| Scan Repo / Run Linter / Revert | `btn-mc-scan` / `-lint` / `-revert` | codebase diagnostics |
| git status | — | live `M`-prefixed modified-file list |

Quality worth preserving — this screen shows more defensive care than any other audited:

- **`_TASK_LIVE_STATUSES` guard.** Editing or deleting an `in_progress` / `running` task is
  refused unless `force=true`, because the goal loop writes its own status back and the edit would
  "appear to work, then silently revert, or the row is recreated"
  ([server.py:9459](../python/harness/server.py#L9459)). A known silent-failure mode, caught and
  guarded, with the reason written down.
- **`paused` has its own colour** (`#bd93f9`) specifically so it does not fall through to pending
  amber and read as "waiting to run", which the comment notes is the opposite of its meaning.
- **Event delegation** is used deliberately, with the polling re-render given as the reason.
- `task.delete` removes children explicitly rather than relying on `ON DELETE SET NULL`.
- Status colours are distinct across pending / in_progress / failed / complete / blocked / paused.

The empty state even names the command to start work: `/goal "..."`.

---


## D7. Verified working — Contacts Directory

`ContactsManager` at [renderer.js:10874](../renderer/renderer.js#L10874).

| Control | id | Behaviour |
|---|---|---|
| + Add Contact | `btn-contacts-add` | opens the Add Contact Profile modal |
| search | `contacts-search-input` | live client-side filter |
| contact list | `contacts-list-container` | cards from `contacts.list`, each badged `PEER` |
| modal fields | `input-add-contact-*` | username, wallet address, Tala endpoint, shared secret |
| Save Contact | `btn-modal-add-contact-save` | `contacts.save` |
| Save Permissions | `btn-contacts-save` | persists the nine sharing toggles |
| Connect | `btn-contacts-connect` | p2p handshake to the contact's endpoint |
| Delete Contact | `btn-contacts-delete` | `contacts.delete` |
| empty states | `contacts-no-selection` | "Select a contact... or create a new contact profile." |

Genuinely solid work here, worth preserving:

- **`escapeHtml()` is used** on both `username` and `wallet_address` when building cards — in
  contrast to the Task Board (UI-029), which omits it on the same kind of DB-sourced string.
- **Packet signing is real.** `_sign_packet` / `_verify_packet` use HMAC-SHA256 over a canonical
  JSON serialisation (`sort_keys=True`, tight separators) and compare with
  `hmac.compare_digest` — constant-time, correctly done.
- **Profile permissions are enforced** generically via `_profile_for_permissions`, so the
  checkboxes are not decorative for profile categories.
- **`nodes` gets a second, independent gate.** `_peer_can_resolve_node_reference` requires both
  the contact permission *and* a firewall access-rights record, with clearance levels and
  allowed-category matching ([server.py:5084](../python/harness/server.py#L5084)). This is the
  model the other eight permissions should follow.
- Modal closes on backdrop click, and the `@` prefix is auto-prepended to usernames.

The nine sharing keys, for the eventual node: `identity`, `about`, `rp_persona`, `pii_vault`,
`roles`, `skills`, `projects`, `connections`, `nodes`.

---


## D8. Verified working — `settings.inference`

Provider card renderer at [renderer.js:1555](../renderer/renderer.js#L1555).

**Row action icons** (needed for the eventual node — these are unlabelled in the UI):

| Icon | Class | Meaning |
|---|---|---|
| ◎ | `btn-provider-route` | Use this provider for the current session |
| ✎ | `btn-provider-edit` | Edit provider |
| ↑ ↓ | `btn-provider-up` / `-down` | Reorder priority (disabled at first/last) |
| ✕ | `btn-provider-delete` | Remove provider |
| 🔒 | `provider-lock-icon` | Built-in — cannot be removed |
| ☐ FORCE | `provider-force-checkbox` | Bypass priority order and fallback chain, except the embedded last resort |

**Model Picker / Active Session Routing** — `Use For Session` (`btn-inference-use-session`) and
`Auto Route` (`btn-inference-auto-route`) are both bound. The status line is computed, not static:
`Session: <name>` when session-routed, `Forced: <name>` when a global force is set, `Auto priority`
otherwise. Setting a force clears `is_forced` on the other providers, so the flag is genuinely
exclusive.

**Local provider health checks are real and model-aware** — see UI-046 for the contrast with
remote. Results are cached per endpoint within a pass, and failure is fail-closed.

**Provider types** render distinct chips: `EMBEDDED` / `LOCAL` / `REMOTE`, with type-appropriate
metadata lines (embedded shows download progress while fetching; local shows model · endpoint ·
engine; remote shows model · service · endpoint).

**Embedded engine is correctly protected** — `embedded-builtin` is skipped by the health loop
entirely, carries `locked: true`, renders the lock icon instead of a delete button, and matches the
panel's stated `Guaranteed fallback` rule.

**Add Inference Provider modal** is engine-aware: choosing Ollama shows Engine / Model Name /
Endpoint URL / API Key *(optional)*; choosing Google Gemini shows Service / Model Name / Endpoint
URL *(optional)* / API Key. Field requiredness flips appropriately between local and remote.

**Engine Manager** offers HuggingFace Transformers (CPU/GPU), vLLM (GPU only — correctly noted as
"Linux or WSL only — there are no Windows wheels"), and llama.cpp (GGUF, GBNF grammar-constrained
sampling). The vLLM caveat is accurate for this platform.

Panel tab switching for all 18 panels is at [renderer.js:711](../renderer/renderer.js#L711).

---


## D9. Verified working — `settings.agent`

Persona manager at [renderer.js:4480](../renderer/renderer.js#L4480).

| Control | id | Behaviour |
|---|---|---|
| card checkbox | — | activates that persona (`is_active`); drives the status-bar `Agent:` readout |
| ✎ / 🗑 | — | edit loads the persona into the form; delete removes it |
| PERSONA NAME | `agent-name-input` | — |
| PERSONA CONFIGURATION TYPE | `agent-type-select` | `Standalone` / `Crew` — **does** reveal the crew fields on change |
| BIRTHDATE (ASTROLOGY) | `agent-birthdate-input` | real, see below |
| PERSONALITY / HISTORY NODE NAME | `agent-background-input` | names an HTML-NN holding backstory |
| AGENT KNOWLEDGE/ASSOCIATION TAG | `agent-tag-input` | HTML-NN with matching tags are isolated to this agent |
| WORKFORCE ROLE | `agent-role-input` | crew-only, revealed by the dropdown |
| CREW CATEGORY | `agent-crew-type-select` | crew-only, revealed by the dropdown |
| SYSTEM DIRECTIVE / PROMPT | `agent-system-prompt` | — |
| Reset / Clear, Save Persona | `btn-agent-reset`, `btn-agent-save` | `agent.save` |

Confirmed live, and worth knowing:

- **The birthdate field is genuinely wired.** [server.py:1375](../python/harness/server.py#L1375)
  reads `config.birthdate`, builds a `Birthdate`, computes transits through `EphemerisEngine`, and
  applies `StyleModulator` to a base style vector — "Chrono-Astrology Style Modulation". Both
  personas carry `1977-07-20`, so this path is active on every turn.
  *Resolved (see UI-066):* `pyswisseph` **is** installed in the active venv, so this path is
  running on the real Swiss Ephemeris on this machine, not the biasing fallback.
- **The persona → HTML-NN links are real.** `Tala Core Identity` and `persona-tala-rp.html` are
  actual nodes; the background node's content is appended to the system prompt as
  `[Personality Background / History Context]` ([server.py:1370](../python/harness/server.py#L1370)).
- **Tag isolation is a genuine feature** — nodes tagged to match are scoped to that agent.
- `Tala Core` renders **without a delete button** while `Tala RP` has one — the seeded core persona
  is protected, consistent with the embedded-engine lock in `settings.inference`.
- Persona cards display type chip, birthdate, backing node, and tag list, so the card is a faithful
  summary of the record.

---


## D10. Verified working — `settings.user profile` chrome

Profile manager wiring at [renderer.js:11217](../renderer/renderer.js#L11217).

| Control | id | Behaviour |
|---|---|---|
| Edit Profile | `btn-profile-edit` | `toggleEditMode(true)` |
| Update Chain | `btn-profile-chain-update` | `updateChainConnection()` |
| Disconnect Chain | `btn-profile-chain-disconnect` | `disconnectChain()`; **correctly disabled** when already local or unset |
| sub-tab nav | `#profile-tabs` | hides all `.profile-tab-panel`, shows `#profile-panel-<tab>` |
| `HUMAN` badge | — | reflects `profile.type`; the code also branches for `agent` |

**Chain Connection card is genuinely live** ([renderer.js:11809](../renderer/renderer.js#L11809)) —
`chain.connection_get` with four distinct rendered states:

- `vps` → "VPS Connected", names the endpoint or username
- `local` → "Local Only", "disconnected from the VPS chain and stores locally"
- unset → "Not Set", "Save your profile, then choose Tala VPS chain or local-only."
- error → "Unavailable" with the actual error message

**Profile Completeness is genuinely computed** ([renderer.js:11960](../renderer/renderer.js#L11960)) —
a ten-item checklist against real config fields, percentage and bar width both derived, and the
tips line lists the first three actually-missing items. It even branches: agent profiles are scored
on `AI Personality` and `Operating Principles`, humans on `Job Title` and `Work History`. The
observed `10%` is consistent with one of ten items filled (display name only).

The checklist covers `identity`, `about`, `rp_persona`, `roles`, `skills`, `projects`,
`connections` — but **not** `pii-vault` or `preferences`, so those two sub-tabs can never affect the
score. Minor, noted for when those sections are audited.

---


## D11. Verified working — user profile sub-sections

**Field structure** (no real values recorded):

| Section | Fields |
|---|---|
| Core Identity | display name, username/handle, profile photo URL, cover banner URL, short bio/headline, location, time zone, preferred language, pronouns, nickname/callsign, motto/tagline |
| About | full biography, personal story/background, current status, goals, values & principles |
| Roleplay Persona | character name, class/occupation/role, faction/affiliation/alignment, backstory & motivation |
| PII & Vault | full legal name, date of birth, primary email, phone number, + `Secure Locations & Document Paths` registry list |
| Roles & Work | current job title, organization/company, department, education & credentials, work history summary |
| Skills & Capabilities | list, `+ Add Skill` |
| Projects & Portfolio | list, `+ Add Project` |
| Connections | list, `+ Add Connection` |
| Preferences & Privacy | tone preference, notification style, + six visibility selectors |

**PII handling in `memory_index` is genuinely well done** — worth preserving, and worth knowing it
is *not* where the leak risk is:

- `index_profile_chunks` tags the PII chunk `trust_label='private'`
  ([memory.py:653](../python/harness/memory.py#L653))
- the search SQL filters it out by default
  ([local_provider.py:51](../sdk/agent/memory/local_provider.py#L51)):
  ```sql
  AND (trust_label != 'private' OR $4::boolean = TRUE)
  ```
- `allow_private` defaults to `False` in `handle_memory_search`, and `research.unified_search` never
  passes it — so the Research view cannot surface PII
- `memory.browse` independently pops `pii_vault` unless `allow_private`
  ([memory.py:318](../python/harness/memory.py#L318))
- `p2p.semantic_search` queries the **node** corpus, not `memory_index` profile chunks

Four independent guards, all correct. The exposure recorded in UI-054 is the *second* copy in the
node file, which none of these cover.

Empty states are all clear and actionable ("No structured skills added. Click Add Skill below.").
Tone preference and notification style persist under `preferences` and `preferences.tone` **is**
consumed — it reaches the profile node and the peer-share payload.

---


## D12. Verified working — `settings.guardrails`

**The content guardrails are genuinely enforced** — worth stating plainly, because most safety-
looking controls audited so far were not.

- `GuardrailsManager.load_current()` then `validate_prompt(prompt)` on the way in
  ([server.py:1208](../python/harness/server.py#L1208))
- `validate_output(generated_text)` on the way out
  ([server.py:3382](../python/harness/server.py#L3382))
- Config persists to `guardrails_config` via `guardrails.get` / `guardrails.save`
- Preset selection drives the rule panel: the four presets set toxicity threshold and toggle rule
  groups exactly as their descriptions claim, confirmed across all four screenshots —
  `None` (all off) → `Lite` (0.80, email + SSN) → `Moderate` (0.50, all four PII fields + SQL) →
  `Heavy` (0.30, + competitors + gibberish, with a pre-populated competitor list)
- `Custom` exists for manual rule configuration
- The `ACTIVE` badge and `Reset Config` / `Save Guardrails` are present and bound

The threshold slider label is accurate ("Lower values block content with lower levels of detected
abuse") and the preset descriptions match the values they actually apply — rare precision in this
codebase.

---


## D13. Verified working — `settings.search`

| Control | Behaviour |
|---|---|
| + Add Search Provider | opens the modal |
| provider card | priority badge, `SEARCH` type chip, name, endpoint |
| FORCE | "bypasses priority order and fallback direct crawl" — genuinely honoured by `SearchManager` |
| ↑ ↓ | reorder priority (disabled at first/last) |
| ✕ | remove provider |
| modal | Common Name, Endpoint URL, **API Key Credential** selector, Custom API Key *(optional)* |

**The credential indirection here is the best pattern in the settings surface** and worth
propagating. `search_providers` carries a `credential_id` foreign key into `auth_credentials`, and
`get_search_providers()` resolves it at read time
([search_manager.py:27](../python/harness/search_manager.py#L27)):

```sql
SELECT p.id, p.name, p.endpoint, p.api_key, p.priority, p.locked, p.credential_id,
       c.config as cred_config
FROM search_providers p
LEFT JOIN auth_credentials c ON p.credential_id = c.id
ORDER BY p.priority ASC
```

A credential is defined once in the AUTH panel and referenced here, with the inline `api_key`
column as a fallback. Compare UI-047, where inference providers store `apiKey` inline in JSONB with
no indirection at all — **this panel already demonstrates the fix for that one.**

Also confirmed working from the Research Library audit: the dropdown there is populated from these
providers, `locked` providers auto-select, and a specific `provider_id` correctly filters
`SearchManager` to that provider alone.

---


## D14. Verified working — `settings.system`

`system.info` at [server.py:7195](../python/harness/server.py#L7195). Genuinely live throughout —
this is a real telemetry panel, not a mock.

| Section | Source |
|---|---|
| Operating System | live OS, release, version, architecture, processor, hostname |
| CPU / Memory / Disk | live percentages with used-vs-total detail and physical/logical core counts |
| Software versions | resolved per binary at call time |
| Python packages | `_pkg_version()` per package |
| Refresh | re-runs the collection |

**Service Status is a genuine probe, not a hardcoded badge** — a welcome contrast with UI-064 and
UI-046:

- **PostgreSQL** — derived from the actual server connection: `"online" if pg_server != "not connected" else "offline"`
- **Blockchain Node** — a real TCP connect to `127.0.0.1:8332` with a 1s timeout, `offline` on any exception ([server.py:7373](../python/harness/server.py#L7373))
- **Python Harness** — reporting is by definition live, since the panel is served by it

A code comment at [server.py:7321](../python/harness/server.py#L7321) records that the GPU section
was added later because "system.info reported CPU/RAM/disk/software but nothing about GPU hardware
or whether local inference" could run — the kind of gap-closing note worth keeping.

---


## D15. Verified working — `settings.git`

| Control | Behaviour |
|---|---|
| + Add Git Provider | opens the modal |
| provider card | priority badge, host type chip (brand-coloured), name, `username @ endpoint` |
| FORCE | checkbox, "bypasses priority order" |
| ↑ ↓ | reorder (disabled at first/last) |
| ✕ | remove |
| modal | Common Name *, Endpoint URL *, Username, **API Key Credential** selector, Custom API Key / PAT |

**Steven's ask is already satisfied at the form level.** The modal takes an arbitrary base URL
("`https://github.com` or `https://gitlab.mycompany.com`"), an arbitrary username, and either a
stored credential or an inline PAT. Nothing constrains it to a known host list, and the display
falls back to a generic `GIT` chip for anything unrecognised. Any git provider can be added today.

**Credential indirection is present here too** — `git_providers.credential_id` was added by
migration ([db.py:1128](../python/harness/db.py#L1128)), matching the good pattern from
`settings.search` (D13) and unlike `settings.inference` (UI-047).

Helper text is accurate throughout: "Leave blank for unauthenticated / public access", "Base URL of
the Git hosting service", "Default username for authenticated operations".

---


## D16. Verified working — `settings.storage`

**Steven's ask is already satisfied at the form level.** The `Provider Type` selector
([index.html:3338](../renderer/index.html#L3338)) offers nine types, including a generic escape
hatch:

`s3` (AWS, MinIO, Backblaze, Spaces — and iDrive e2, which is S3-compatible) · `gdrive` · `nfs` ·
`azure` · `sftp` · `ftp` · `dropbox` · `onedrive` · **`custom`**

Every provider Steven named is present. The form is also **type-aware** — selecting S3-Compatible
reveals an `S3 CONFIGURATION` block (Endpoint URL, Region, Bucket Name, Key Prefix, Access Key ID,
Secret Access Key) with required-field marking, and other types swap in their own field sets.

Helper text is accurate and specific ("blank for AWS", "optional folder prefix", `AKIA...`
placeholder, `us-east-1` region default). The empty state names the exact button to click.

Priority ordering, `locked`, save and load all round-trip correctly through
`storage.get_providers` / `storage.save_providers`.

The gap is entirely below the form: nothing reads what it saves, and no client exists to use it
(UI-068, UI-069).

---


## D17. Verified working — `settings.firewall`

**Category Restrictions genuinely work** — the one half of this panel that enforces.

`_peer_can_resolve_node_reference` ([server.py:5084](../python/harness/server.py#L5084)) requires
**both** the contact's `nodes` permission **and** a `firewall_contacts` record, then:

- honours a `*` / `all` wildcard as a fast path
- checks `allowed_categories` and `allowed_node_names` via `_firewall_allows_node_name`
- falls back to reading the node's own `state_data.tags` / `categories` / `category`, plus its
  `node_type`, and matches against the allowed set — **and nodes do carry tags**, so this comparison
  has real data on both sides
- returns a specific denial reason (`contact_nodes_permission_disabled`, `firewall_denied`,
  `clearance_level_insufficient: N < M`) rather than a bare boolean

The modal collects Display Name, Public Key / Network Address, Clearance slider `[0-10]`, four
category checkboxes (RP / CODING / IDENTITY / WORKFLOWS DATA), and free-text
`ALLOWED NODE NAMES / COLLECTIONS`.

This remains the **strongest access-control model in the app** — the two-gate design (contact
permission AND firewall record) is the pattern recommended in UI-062 for the other eight contact
permissions.

---


## D18. Verified working — `settings.logging`

The richest telemetry surface in the app, and mostly real.

| Element | Status |
|---|---|
| TOTAL EVENTS / ERRORS / WARNS / SESSIONS / TURNS | live SQL counts over `agent_log` and `token_ledger` |
| SYSTEM HEALTH — `OVERALL` + `MODE` | live via `logging.get_health`; reports `DEGRADED` / `DEGRADED_AUTONOMY` honestly rather than defaulting to green |
| 12 subsystem indicators | **only 3 are computed — see UI-091** |
| SOURCE / SUBSYSTEM filters | dropdowns |
| ERROR / WARN / INFO / DEBUG / ERRORS ONLY | severity filters |
| search, REFRESH, LIVE TAIL, EXPORT | present and bound |
| ARCHIVE CURRENT / ALL, CLEAR CURRENT / ALL | `logging.archive_events` / `logging.clear_events` |
| row detail pane | right-hand panel for the selected entry |

> **Correction (same session).** This entry first credited the health panel with "reporting a bad
> state truthfully". That was wrong — see **UI-091**. Nine of the twelve subsystem indicators are
> hardcoded string literals, including both that read `DEGRADED`. The panel reports a *fixed*
> state, not a live one.

It also honestly discloses its own cap — *"Showing most recent 500 entries"* — in contrast with the
Task Board's silent `LIMIT 50` (UI-030).

---

## E. Notes on method

- Steven's label for a region is recorded verbatim alongside the element id. His naming is the
  user-facing truth; the id is the mechanism. Both belong in the eventual node.
- Every control gets an explicit **works / partial / dead** verdict with the evidence line, so
  the map doubles as a defect list.
- Expect more wired-but-never-invoked controls. That is this codebase's dominant failure mode,
  and UI-004/005/006 confirm the pattern is present in the chat header.

# UI Audit — Decision Queue

Every open question from [ui-audit-findings.md](./ui-audit-findings.md), ordered so that answering
them top-to-bottom never wastes work. **49 open** — Q-4, Q-8, Q-9, Q-10 and Q-45 are already
answered and are not repeated here.

These gate the build. Functionality has to exist before the UI is worth mapping into `ui-view-*`
nodes for self-navigation.

- **Who decides** — `Steven` means only you can answer it. `Claude` means I can answer it by
  checking, and it needs no decision from you.
- **Cost** — rough effort *to answer*, not to build.
- Write answers inline under each question. This file is the worksheet.

---

## P0 — Answer first: cheap, and they change what the other 45 mean

Four questions. None needs design thought, and each collapses a chunk of the list.
**One is now answered.** Three remain, plus one new follow-up (Q-55).

### ~~Q-44 + Q-30~~ — Which directory is the running app launched from? **ANSWERED**
**Blocks:** UI-056 · **Status:** closed 2026-08-23 · **Detail:** UI-095

> **Steven:** *"This directory should be the directory TALA is installed to. The intent is for TALA
> to be self sustaining and work from her own directory, this way she could be installed on a USB
> stick and moved."*

**That is what it does today.** `TALA_USER_DATA` is unset at every scope, so all three resolvers
fall through to the install directory, and `firstRunEnv()` injects the resolved root into the
Python child so they cannot drift. The install-dir `nodes/` holds the newest writes (today 10:39);
the `%APPDATA%` copy is 16 days stale and the packaged copy 2.5 months stale.

**Knock-on:** UI-056's divergence hypothesis is weakened — only one root is live. The remaining
suspect for *"after a few restarts it goes away"* is the **incomplete DB sync**, which never writes
Roles & Work, Skills, Projects, Connections, privacy, or most of Core Identity and About. One
follow-up added: **Q-55**.

### ~~Q-55~~ — Should the profile DB sync write the full config? **ANSWERED**
**Blocks:** UI-056 · **Status:** closed 2026-08-23 · **Detail:** UI-099

> **Steven:** *"sync should write everything"*

Agreed, and tracing it found the real mechanism — **the DB sync is not the bottleneck**. The panel
calls `profiles.save`, which already sends and stores the full nested config; the narrow 13-field
endpoint I blamed in UI-056 is not in the panel's path at all.

The actual defect: **the live `concept-user-profile.html` is still in the flat legacy format** with
no `config` dict, so every load takes the "best-effort, always-incomplete" reconstruction branch and
~22 fields have nothing to load from. Corrected in UI-099.

**One thing to confirm before fixing (Q-57):** if the panel calls `profiles.save` and that writes
the full config, why is the node still flat? A migration alone will not help if the writer is
broken.

### Q-57 — Why is the profile node still flat if `profiles.save` writes the full config?
**Blocks:** UI-099 · **Who:** Claude (diagnostic) · **Cost:** minutes · *new, from the Q-55 answer*

Either `profiles.save` has not completed successfully since the format changed, or its node-write
path is not reached. I can determine this by saving a profile and re-reading the node. Needs doing
**before** any migration, since migrating a node a broken writer will re-flatten achieves nothing.

> **Answer:**

### Q-16 — Has the Task Board ever held real, non-test tasks?
**Blocks:** UI-030, UI-031 · **Who:** Steven · **Cost:** one sentence

If it has only ever held fixtures, the 50-row cap and the payload-decode risk have never been
exercised, and both drop several priority levels.

> **Answer:**

### Q-15 — Purge the test fixtures from the live Task Board and `nodes/`, or keep as sample data?
**Blocks:** UI-028 · **Who:** Steven · **Cost:** one sentence

Independent of the test-isolation fix. Right now the board cannot be read as a view of real work.

> **Answer:**

### Q-48 — Keep messaging channels at all?
**Blocks:** UI-083, UI-084 · **Who:** Steven · **Cost:** one sentence

At P0 because **"no" deletes the single most exposed surface in the audit** — an unenforced
allowlist sitting in front of tool execution — at zero engineering cost. "Yes" moves UI-083 to P1.

> **Answer:**

---

## P1 — Security exposure, live right now

Six questions. Each corresponds to a gap a peer could use today.

### Q-22 — Should an enrolling peer's `requested_permissions` await local approval?
**Blocks:** UI-041 · **Who:** Steven · **Cost:** low

Today a peer holding the enrollment secret writes its own permissions verbatim, and
`ON CONFLICT DO UPDATE` lets it overwrite permissions you previously restricted. This one change
makes your checkboxes authoritative.

> **Answer:**

### Q-21 — Should `p2p.marketplace_search` require an identified, permitted caller?
**Blocks:** UI-040 · **Who:** Steven · **Cost:** low

It is the one p2p path with no caller identity at all, and it returns the **full body** of every
file in `skills/`. The `share-skills` checkbox does not gate it.

> **Answer:**

### Q-23 — Per-skill sharing rather than one global `skills` toggle?
**Blocks:** UI-040 · **Who:** Steven · **Cost:** low

E.g. a `shared: true` flag in each `skills/*.yaml`. Shapes the fix for Q-21.

> **Answer:**

### Q-47 — Is `gates.py` using credential *name + priority=1* as an authorization allowlist intended?
**Blocks:** UI-081 · **Who:** Steven · **Cost:** low

Renaming a credential, or reordering the AUTH priority chain, silently changes what actions Tala is
permitted to take. Nothing in the UI hints at that.

> **Answer:**

### Q-32 — Privacy visibility vs Contacts permissions: which surface wins?
**Blocks:** UI-058, UI-040 · **Who:** Steven · **Cost:** medium

Two access-control surfaces with different keys. The six Preferences dropdowns are read by nothing,
so setting them to Private gives no protection under a heading that says "access control".

> **Answer:**

### Q-46 — Should nodes carry a `clearance` value in their `node-state` block?
**Blocks:** UI-082 · **Who:** Steven · **Cost:** medium

0 of 123 nodes declare one, so the firewall clearance slider cannot deny anything. Which nodes
should sit above 0 — PII, vault, identity?

> **Answer:**

---

## P2 — Architecture forks: answering late means building the wrong thing

Nine questions. Each is a case where a working subsystem already exists and a second one is
half-built alongside it. Building before deciding duplicates an engine.

### Q-28 — Are crews a separate orchestration concept, or a front-end onto swarm topologies?
**Blocks:** UI-049, UI-050, UI-051 · **Who:** Steven · **Cost:** medium

**The highest-leverage question in P2.** Topologies already do hierarchy, distinct functions and
execution, and `TopologyNode.agent_id` already binds a node to a persona. Building crew hierarchy
from scratch duplicates a working engine.

> **Answer:**

### Q-29 — If crews stay separate: does a boss *route work to* subordinates, or merely *own* them?
**Blocks:** UI-049 · **Who:** Steven · **Cost:** low · *Only if Q-28 says "separate"*

Routing means reusing the swarm executor. Ownership is just a tree.

> **Answer:**

### Q-43 — `settings.runtime`: remove in favour of Contacts, make it a view onto it, or keep anonymous?
**Blocks:** UI-075, UI-076 · **Who:** Steven · **Cost:** low

Contacts + p2p already fetches nodes from a peer, signed and permission-gated. Your own phrasing
for this panel — *"contact endpoint/username"* — describes Contacts, not this.

> **Answer:**

### Q-14 — `python/ledger/` vs `blockchain/src/app.js`: delete the Python one, or promote it?
**Blocks:** UI-027 · **Who:** Steven · **Cost:** low

Two complete chain servers on the same port. The Python one is dead but has a passing test, so it
will not show up as rot. Its default tx type `html_nn_anchor` is closer to a Real-Chain anchoring
model than the live chain's `node-create`.

> **Answer:**

### Q-11 — Ghost vs Real: one chain with tagged transactions, or two chains that reconcile?
**Blocks:** UI-023, UI-024 · **Who:** Steven · **Cost:** medium

Either way this is a **schema change before a UI change** — neither `token_ledger` nor the chain
transaction records which mode was active, so there is currently nothing to filter on.

> **Answer:**

### Q-12 — What actually belongs on the Real Chain?
**Blocks:** UI-024 · **Who:** Steven · **Cost:** medium

100% of current traffic is `node-create`, which by your own definition — "non-value... tracking
locally" — sounds Ghost-only.

> **Answer:**

### Q-35 — Should guardrails absorb contacts-permissions and firewall into one policy object?
**Blocks:** UI-061, UI-062 · **Who:** Steven · **Cost:** high

Your framing says absorb. Three systems exist today with separate storage, vocabulary and
enforcement points; only node resolution combines two of them, and that pairing is the prototype
worth copying.

> **Answer:**

### Q-36 — What is the subject of a policy: a contact, an agent, or a (contact, agent) pair?
**Blocks:** UI-062 · **Who:** Steven · **Cost:** medium

Your example implies the pair, which is the most expressive and the most work.

> **Answer:**

### Q-37 — "A certain port": outbound calls Tala makes, or an inbound port a peer may reach?
**Blocks:** UI-063 · **Who:** Steven · **Cost:** low

Opposite enforcement points, built in different places. No port concept exists anywhere yet;
`leases.py` already has the TTL machinery for the "single allocated time" half.

> **Answer:**

---

## P3 — Capability gaps: this tier is what "so the functionality is there" means

Twelve questions. Each is a place where Tala currently *cannot do* something the UI implies she
can. This is the real precondition for self-navigation.

### Q-56 — Which platforms must MCP discovery support, and is a per-OS path table wanted?
**Blocks:** UI-097 · **Who:** Steven · **Cost:** low · *new, raised by Steven 2026-08-23*

7 of 8 discovery locations can never match on Linux or macOS — `%VAR%` syntax is not expanded by
POSIX `expandvars`, and backslashes are literal filename characters there. The failure is silent:
zero servers found looks identical to zero servers installed. Fixing it needs real per-OS paths
(Claude Desktop lives in `~/Library/Application Support/` on macOS, `~/.config/` on Linux), not
just syntax repair.

> **Answer:**

### Q-34 — `workflow.execute` as a tool, or a generic RPC-invoke tool?
**Blocks:** UI-060 · **Who:** Steven · **Cost:** low

The compiled workflow node *tells* Tala to call `workflow.execute`, and she has no mechanism to.
A generic RPC-invoke tool would expose every method in the harness — decide deliberately.

> **Answer:**

### Q-50 — Which log surface does Tala get: read-only, or also archive/clear?
**Blocks:** UI-086 · **Who:** Steven · **Cost:** low

Self-diagnosis needs `get_events` + `get_health`. The other two let her erase her own audit trail.

> **Answer:**

### Q-49 — Bridge `logger.*` into `agent_log`, or convert call sites individually?
**Blocks:** UI-085 · **Who:** Steven · **Cost:** low

A logging handler catches all 257 sites and every future one. Today `agent_log` is written at INFO
and WARNING only — never ERROR, and never from an RPC failure path.

> **Answer:**

### Q-7 — Is watching Tala browse a requirement, or is headless fine if she reports back?
**Blocks:** UI-013 · **Who:** Steven · **Cost:** low · *Answer before Q-6*

Decides whether UI-013 is a defect or a design choice.

> **Answer:**

### Q-6 — Re-point Tala's browser tools at the visible webview, or have the webview mirror Playwright?
**Blocks:** UI-013, UI-016 · **Who:** Steven · **Cost:** medium

> **Answer:**

### Q-19 — "Local LLM could reference" the vault: does that mean Ollama-only?
**Blocks:** UI-038 · **Who:** Steven · **Cost:** low

The isolation guard refuses vault reads whenever network is active, so a remote provider could
never read it by design.

> **Answer:**

### Q-18 — Vault scope: plain files, or HTML-NN documents like everything else?
**Blocks:** UI-036, UI-037 · **Who:** Steven · **Cost:** medium

Decides the whole build. Your universal-data mandate points one way; "editable private documents"
sounds like arbitrary files.

> **Answer:**

### Q-20 — Should the vault be a source in Unified Search?
**Blocks:** UI-038 · **Who:** Steven · **Cost:** low

Alongside Web / Chat Sessions / Neural Nodes / Tasks.

> **Answer:**

### Q-31 — Profile into every system prompt, or retrieved on demand?
**Blocks:** UI-057 · **Who:** Steven · **Cost:** low

Always-inject costs tokens every turn; retrieval means it is sometimes absent. Today it is never
present — the profile is wired to be shared with peers and not to be used by your own agent.

> **Answer:**

### Q-33 — Workflow palette from the tool registry, from HTML-NN nodes, or both?
**Blocks:** UI-059 · **Who:** Steven · **Cost:** medium

Your framing says HTML-NN; tools are what actually execute. Five hardcoded types today, with 66
tools and 123 nodes unreachable.

> **Answer:**

### Q-40 — Which storage backends are needed first?
**Blocks:** UI-068 · **Who:** Steven · **Cost:** low

S3-compatible alone covers AWS, MinIO, Backblaze, Spaces **and iDrive e2** — five of your named
targets for one dependency.

> **Answer:**

---

## P4 — Truth-in-labelling and scope: cheap, and two are needed for the map itself

Nineteen questions. Mostly one-liners, but **Q-2 and Q-5 gate node authoring directly** — the map
records your wording, and I do not have it yet.

### Q-2 — The green dot beside **AGENT CHAT**: what does it indicate?
**Blocks:** landing node · **Who:** Steven · **Cost:** one line · **Needed for the map**

> **Answer:**

### Q-5 — `Exec:` / `Mem:` card chips: your day-to-day meaning, not the attribute names?
**Blocks:** landing node · **Who:** Steven · **Cost:** one line · **Needed for the map**

> **Answer:**

### Q-1 — Status-bar token field: wallet balance, or usage meter?
**Blocks:** UI-001, UI-002, UI-003 · **Who:** Steven · **Cost:** one line

You said blockchain tokens; the code sums `token_ledger` consumption. Recommendation on file:
re-point at `get_balance()`, which also makes the dead `Ghost Tokens` infinity branch reachable.

> **Answer:**

### Q-3 — `MOOD` / `MD` / `JSON`: wire them up, or delete them?
**Blocks:** UI-004, UI-005, UI-006 · **Who:** Steven · **Cost:** one line

> **Answer:**

### Q-17 — Retention for finished tasks: auto-hide after N days, a toggle, or archive-on-done?
**Blocks:** UI-034 · **Who:** Steven · **Cost:** low

Interacts with the 50-row cap: once 50 stale tasks accumulate, new ones stop appearing at all.

> **Answer:**

### Q-24 — `APPLY CHANGES`: make settings staged-then-applied, or remove the button?
**Blocks:** UI-045 · **Who:** Steven · **Cost:** low

Panels already save on change, so the button currently teaches the wrong mental model.

> **Answer:**

### Q-25 — `USB (ISOLATED)` / `LOCAL` / `REMOTE`: what should each actually do?
**Blocks:** UI-044, UI-039 · **Who:** Steven · **Cost:** medium

It reads as a security control and does nothing. Should it drive `isNetworkIsolated()`?

> **Answer:**

### Q-26 — Is `GLOBAL` vs `WORKSPACE` scope real, or leftover chrome to delete?
**Blocks:** UI-044 · **Who:** Steven · **Cost:** one line

> **Answer:**

### Q-27 — Embedding-only providers: hide, grey out, or move to their own section?
**Blocks:** UI-048 · **Who:** Steven · **Cost:** one line

They occupy priority slots 6 and 7 but are filtered out of the chat chain, so the displayed order
is not the effective order.

> **Answer:**

### Q-38 — Search providers: real health probe, or remove the badge?
**Blocks:** UI-064 · **Who:** Steven · **Cost:** one line

`ACTIVE` is hardcoded with no variable behind it. Nothing anywhere would reveal a dead provider.

> **Answer:**

### Q-53 — Nine hardcoded subsystem statuses: wire to real probes, or show only the three that are real?
**Blocks:** UI-091 · **Who:** Steven · **Cost:** low

A permanently-`DEGRADED` banner trains you to ignore it.

> **Answer:**

### Q-54 — Is `mem0` a real dependency, or should "Dual Memory" drop the mem0 half?
**Blocks:** UI-093 · **Who:** Steven · **Cost:** one line

It is an optional extra and is not installed in the venv.

> **Answer:**

### Q-51 — Is the architecture diagram aspirational or descriptive?
**Blocks:** UI-088 · **Who:** Steven · **Cost:** one line

Four of nine boxes are only true aspirationally. A proposal can be either — not silently both.

> **Answer:**

### Q-52 — Should the diagram gain a Filesystem / HTML-NN Node Store box?
**Blocks:** UI-089 · **Who:** Steven · **Cost:** one line

It is the project's defining component and is currently absent, with its role misattributed to
PostgreSQL.

> **Answer:**

### Q-39 — Do clone/push need building, or is Tala only meant to `pull` its own repo?
**Blocks:** UI-067 · **Who:** Steven · **Cost:** one line

Decides whether UI-067 is a large build or a documentation correction.

> **Answer:**

### Q-41 — Is off-machine backup of `nodes/` a real requirement?
**Blocks:** UI-069, UI-056 · **Who:** Steven · **Cost:** one line

The storage panel currently *implies* a safety net that does not exist — relevant given UI-056.

> **Answer:**

### Q-42 — Multi-database routing / read-replica failover: wanted, or is `settings.DB_URL` the single source?
**Blocks:** UI-071, UI-072 · **Who:** Steven · **Cost:** one line

Failover is architecturally impossible as built — one pool, one DSN, no standby.

> **Answer:**

### Q-13 — Is `https://talacore.app` the intended Real Chain host?
**Blocks:** UI-025 · **Who:** Steven · **Cost:** one line

Already allow-listed in the Electron CSP.

> **Answer:**

---

## Suggested working order

1. **P0 — 4 questions.** I answer Q-44/Q-30 by checking; you answer three one-liners. This may
   remove the data-loss bug and the most exposed security surface before any code is written.
2. **P1 — 6 questions.** Close live exposure. Q-22 alone makes your permission checkboxes
   authoritative.
3. **P2 — 9 questions.** Settle the forks. **Q-28 first**: it decides whether crews are a build or
   a view onto an engine that already works.
4. **P3 — 11 questions.** Build the capabilities. This tier is the precondition for
   self-navigation.
5. **P4 — 19 questions.** Labelling and scope. Do **Q-2 and Q-5 early regardless**: the map needs
   your wording and they cost a line each.

**P0 and P1 together are 10 questions, mostly one-liners.** That is the smallest set that makes the
system safe to keep building on.

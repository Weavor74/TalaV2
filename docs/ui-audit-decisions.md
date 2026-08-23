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

**P0 is complete — all six closed 2026-08-23.** Between them they located the profile data-loss bug
(UI-099, UI-100), confirmed the portable data root works (UI-095), and cleared the test fixtures.

**Data status: nothing was lost.** The full profile is intact in `profiles.config`; only the node
mirror is stale.

### ~~Q-44 + Q-30~~ — Which directory is the running app launched from? **ANSWERED**
**Blocks:** UI-056 · **Status:** closed 2026-08-23 · **Detail:** UI-095

> **Steven:** *"This directory should be the directory TALA is installed to. The intent is for TALA
> to be self sustaining and work from her own directory, this way she could be installed on a USB
> stick and moved."*

**That is what it does today.** `TALA_USER_DATA` is unset at every scope, so all three resolvers
fall through to the install directory, and `firstRunEnv()` injects the resolved root into the
Python child so they cannot drift. The install-dir `nodes/` holds the newest writes (today 10:39);
the `%APPDATA%` copy is 16 days stale and the packaged copy 2.5 months stale.

**Knock-on:** UI-056's divergence hypothesis is weakened — only one root is live. That pointed the
search at the profile save path, and Q-55 below found the real cause: the node on disk is in the
legacy flat format (UI-099).

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

### ~~Q-57~~ — Why is the profile node still flat? **ANSWERED**
**Blocks:** UI-099 · **Status:** closed 2026-08-23 · **Detail:** UI-100

`profiles.save` writes the DB successfully — the config holds identity(12), roles(10), privacy(6),
about(5), skills(5), projects(4), connections(1). **The data is not lost.** The *node* write sits in
a try/except that logs a warning and lets the save return success, and the loader reads only the
node, never the DB. No trace in `agent_log` or any log file. Recovery: regenerate the node from
`profiles.config`.

### ~~Q-16~~ — Has the Task Board ever held real tasks? **ANSWERED**
**Blocks:** UI-030, UI-031 · **Status:** closed 2026-08-23

> **Steven:** *"No, all tasks were either generated testing or generated planned prompts."*

UI-030 (50-row cap) and UI-031 (payload decode) have therefore never been exercised in anger —
both drop in priority.

### ~~Q-15~~ — Purge the test fixtures? **ANSWERED — DONE**
**Blocks:** UI-028 · **Status:** closed 2026-08-23

> **Steven:** *"Purge"*

Executed: `tasks` truncated, 13 -> 0. Junk learning nodes pruned with the project's own
`scripts/prune_junk_learning_nodes.py --apply` — 8 removed, 3 genuine learning nodes kept, 123 ->
115 nodes. **UI-028's root cause is untouched**: tests still write to the live DB, so fixtures will
return on the next `pytest` run.

### ~~Q-48~~ — Keep messaging channels? **ANSWERED — YES**
**Blocks:** UI-083, UI-084 · **Status:** closed 2026-08-23

> **Steven:** *"yes. I'm considering adding messaging even though I want to do custom app"*

Channels stay, so **UI-083 is now a live must-fix** — the allowlist is collected, stored, and never
read, in front of tool execution. UI-084 (no Discord adapter, yet Discord is the dropdown default)
also becomes real work rather than dead code to delete.

---

## P1 — Security exposure, live right now

**All six answered 2026-08-23**, plus follow-ups Q-58 and Q-59. Read together they define a single
access-control model that does not exist yet — see **The access model** below.

**Still open in P1:** **Q-61** only — what `AuthorizationGate` should check once credential priority
no longer confers authority. **Deferred:** Q-60 (classification taxonomy), at Steven's request.

### ~~Q-22~~ — Should an enrolling peer's `requested_permissions` await approval? **ANSWERED**
**Blocks:** UI-041 · **Status:** closed 2026-08-23

> **Steven:** *"yes, permissions are by invite only. Example: in order for my wife to gain access, I
> have to invite her to access. This does not mean I have to invite her to be a contact. Just to
> access a file."*

**Grants are per-resource and independent of contact status.** Being a contact establishes identity
and transport; it confers no access. Access is a separate, explicit invitation to a specific thing.

This is a capability model, not a role model, and the current schema cannot express it —
`contacts.permissions` is nine booleans scoped to the *contact*, with no resource dimension.

### ~~Q-21~~ — Should `p2p.marketplace_search` require an identified, permitted caller? **ANSWERED**
**Blocks:** UI-040 · **Status:** closed 2026-08-23

> **Steven:** *"Yes"*

The handler currently takes no caller identity at all and returns the full body of every file in
`skills/`. It needs both an identified caller and a permission check, per Q-23's per-skill scope.

### ~~Q-23~~ — Per-skill sharing rather than one global toggle? **ANSWERED**
**Blocks:** UI-040 · **Status:** closed 2026-08-23

> **Steven:** *"Yes per skill sharing. I should be able to choose, global or contact only. Global
> allows everyone to access, contact only allows one."*

Each skill carries its own visibility: **`global`** (any peer) or **`contact-only`** (named
grantees). Replaces the single `share-skills` contact flag, which cannot express either.

### ~~Q-47~~ — Is credential *name + priority=1* as authorization intended? **ANSWERED — NO**
**Blocks:** UI-081 · **Status:** closed 2026-08-23

> **Steven:** *"No. Priority should only be a fallback method for authentication. Reason is we may
> have an API, or an OAuth. Those are = priorities."*

Priority orders **authentication fallback** only — try this credential, then that one. It carries no
authorization meaning, and alternative credential types for the same service are peers, not a
ranking. `gates.py` treating `priority = 1` as an allowlist is a bug, not a design.

**Leaves open (Q-58):** what *should* authorize an action, once priority no longer does.

### ~~Q-32~~ — Privacy visibility vs Contacts permissions? **ANSWERED**
**Blocks:** UI-058, UI-040 · **Status:** closed 2026-08-23

> **Steven:** *"If you give a contact permissions it supersedes other settings, unless a lockdown is
> initiated, then no access anywhere."*

Precedence, highest first:

1. **Lockdown** — see Q-59: a **network cut**, not a permission deny. It stops peer traffic
   outright, so no grant can be exercised remotely; local agent work is unaffected
2. **Contact grant** — an explicit grant supersedes the profile visibility setting
3. **Profile visibility** — the default when no grant exists

Q-59 scoped lockdown: it is a network-layer cut. The flag and guard already exist but nothing ever
enables them — UI-102.

### ~~Q-46~~ — Should nodes carry a `clearance` value? **ANSWERED**
**Blocks:** UI-082 · **Status:** closed 2026-08-23

> **Steven:** *"The clearance value should be determined by group settings of the node. This may be
> something we think of now such as Classified, PII, or Public."*

Not a hand-typed number per node — a **classification the node belongs to**, which maps to a
clearance level. Named examples: `Public`, `PII`, `Classified`.

This fits the existing enforcement well: `_peer_can_resolve_node_reference` already reads the node's
own tags/categories, so classification can ride the same path rather than needing a new field.
The taxonomy itself is Q-60.


### The access model these six answers define

Steven's six P1 answers are not six independent decisions. They describe one coherent design:

| Principle | From | Status in code |
|---|---|---|
| Identity and access are **separate** — being a contact grants nothing | Q-22 | **not expressible**; `contacts.permissions` has no resource dimension |
| Access is granted **per resource, by invitation** | Q-22 | not implemented |
| A shared item carries its own scope: `global` or `contact-only` | Q-23 | not implemented; one global `skills` flag exists |
| Callers must be **identified before** being permitted | Q-21 | `p2p.marketplace_search` takes no identity at all |
| Node sensitivity comes from a **classification group**, not a per-node number | Q-46 | enforcement path exists and already reads tags — the taxonomy does not |
| **Lockdown** overrides every grant | Q-32 | concept does not exist anywhere |
| Credential priority is **authentication fallback only**, never authorization | Q-47 | `gates.py` currently uses it as authorization |

**This substantially pre-answers Q-35 and Q-36 in P2.** Q-35 asks whether guardrails should absorb
contacts-permissions and firewall into one policy object; the model above *is* that object. Q-36
asks what a policy's subject is; Q-22 answers it — the subject is a **(grantee, resource)** pair,
not a contact and not an agent.

Worth revisiting both when P2 is reached, rather than treating them as open from scratch.

### ~~Q-58~~ — What authorizes an action now that priority does not? **ANSWERED (intent)**
**Blocks:** UI-081 · **Status:** intent settled 2026-08-23 · replacement still open

> **Steven:** *"This was only meant for order of operations not execution authority."*

Confirms priority was **never** meant to confer authority — `gates.py` reading `priority = 1` as an
allowlist is a straight misuse, not a design being replaced.

**Still undefined:** `AuthorizationGate` currently *denies* unless it finds that credential, so
removing the check leaves the gate with no basis at all. Narrowed to **Q-61** rather than left
inside this question.

### ~~Q-59~~ — Lockdown scope, trigger, release? **ANSWERED**
**Blocks:** UI-058 · **Status:** closed 2026-08-23 · **Detail:** UI-102

> **Steven:** *"It does not block local agent. It hardlines a network disconnect so no traffic can
> act. Only conversation between Harness and LLM."*

Lockdown is a **network-layer cut, not a permission-layer deny** — which is a different mechanism
from the precedence chain in Q-32. Local agent work continues; only harness↔LLM traffic survives.

**This is already half-built and currently broken shut** — see UI-102. `isNetworkIsolated()` exists
and gates `vault:read`, but nothing in the codebase ever sets it `true`.

**One tension to resolve (Q-62):** if the active provider is a *cloud* API (Gemini, Kimi3), then
harness↔LLM traffic **is** network traffic. Either lockdown forces local inference, or it must
carve out an exception for the provider endpoint.

### Q-60 — Classification taxonomy and clearance mapping **DEFERRED**
**Blocks:** UI-082 · **Who:** Steven · **Status:** deferred by Steven 2026-08-23

> **Steven:** *"We need to come back to this for deeper conversation."*

Parked deliberately, not unanswered. Blocks UI-082 (the firewall clearance slider stays inert until
a taxonomy exists) but blocks nothing else, so it does not hold up P2.

Open when resumed: is `Public / PII / Classified` the full set or extensible; what each maps to on
the existing `[0-10]` scale; and what classifies the **115 existing nodes**, none of which carry a
classification today.

> **Answer:**

### Q-61 — What should `AuthorizationGate` actually check?
**Blocks:** UI-081 · **Who:** Steven · **Cost:** medium · *narrowed from Q-58*

Priority is confirmed as ordering only, so the credential lookup comes out. But the gate currently
**denies by default** unless it finds that credential — remove it and the gate either blocks
everything or nothing. Needs a real basis: an explicit action allowlist, a capability grant under
the Q-22 model, or a risk-tier rule.

> **Answer:**

### ~~Q-62~~ — Under lockdown, what happens with a cloud provider? **ANSWERED**
**Blocks:** UI-102 · **Status:** closed 2026-08-23

> **Steven:** *"The lockdown overrides forced model and drops to local only models."*

Lockdown outranks the `FORCE` flag and restricts selection to local providers. Clean, and it
resolves the contradiction — lockdown never permits outbound inference, so "no traffic can act"
holds literally.

**Implementation surface is narrow.** Provider selection already reads
`config.is_forced` ([server.py:2433](../python/harness/server.py#L2433)) over a chain ordered by
priority, with a documented precedence: force beats priority, except the embedded engine is always
the last resort. Lockdown adds one tier above force, filtering the chain to `type in (local,
embedded)`.

**There is already a guaranteed floor.** `embedded-builtin` is `locked`, CPU-only, and "always
available", so lockdown can never leave the system with no model — even if Ollama is down.

**Knock-on for UI-046:** remote providers report `ONLINE` purely because an API key string exists.
Under lockdown they are unreachable by design but would still render green. The badge should
reflect lockdown state, or the fix for UI-046 should account for it.

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

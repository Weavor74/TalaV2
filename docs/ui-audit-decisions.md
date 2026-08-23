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

**P1 is complete.** One follow-up remains open — **Q-63** (which principal the gate resolves
against). **Deferred:** Q-60 (classification taxonomy), at Steven's request.

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

### ~~Q-61~~ — What should `AuthorizationGate` check? **ANSWERED**
**Blocks:** UI-081 · **Status:** closed 2026-08-23 · **Detail:** UI-103

> **Steven:** *"This should check the user contacts, and firewall/guardrail settings."*

Makes `AuthorizationGate` the **consumer of the unified policy object** from UI-101 rather than
holding its own logic — which is consistent with Q-35's direction and closes the loop on UI-062.

**Two things to know before building it** (UI-103):

1. **The gate is never invoked.** `request_authorization()` has no callers outside its own
   definition, so the `priority = 1` bug is latent rather than active. The work is *wiring it in*
   as much as giving it a basis.
2. **Its signature already fits.** `request_authorization(action_type, resource, risk_class)` is
   already resource-scoped, matching the *(grantee, resource)* subject from Q-22. Risk tiers already
   short-circuit: `low` auto-allows, `high`/`critical` demand proof, `medium` defaults open.

**One axis to resolve:** contacts and firewall govern what a **peer** may read from you.
`AuthorizationGate` governs what **Tala** may execute locally. Those are different principals, so
the gate needs to know *who is asking* — a peer-triggered action should resolve against that peer's
grant, while an action you initiate locally should not consult contacts at all. Raised as **Q-63**.

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

### Q-63 — Which principal does `AuthorizationGate` resolve against?
**Blocks:** UI-081, UI-103 · **Who:** Steven · **Cost:** low · *from Q-61*

Contacts and firewall describe what a **peer** may read from you; the gate governs what **Tala**
may execute. A peer-triggered action should resolve against that peer's grant — but an action you
start yourself has no contact to check. Does the gate treat the local owner as implicitly
authorized, with contacts consulted only for peer-originated work?

> **Answer:**

---

## P2 — Architecture forks: answering late means building the wrong thing

**All nine answered 2026-08-23.** Two of my recommendations were overturned: crews are *not* a
front-end onto topologies (Q-28), and Ghost/Real are *not* one chain with tagged transactions
(Q-11). One follow-up: **Q-64**.

### The policy object

Q-35 (absorb), Q-36 (pair) and Q-37 (both) combine with Q-22 (per-resource) into one shape:

> **(contact, agent) -> resource -> constraints**

*"This person, acting through this agent, may reach this resource, on these ports, within this
window."* — exactly the example Steven opened the guardrails panel with. Constraints so far:
**ports** (inbound and outbound, Q-37) and a **time window** (the original example; `leases.py`
already has TTL machinery).

Precedence from Q-32: **lockdown -> contact grant -> profile visibility**.

### Q-64 — Are the existing "swarm" topologies actually crews?
**Blocks:** UI-049, UI-051 · **Who:** Steven · **Cost:** low · *from Q-28*

By Q-28's definition a swarm is many agents doing *the same* job. But `Code Repair Swarm` is
`planner -> coder -> tester -> reviewer -> documenter` — five different jobs, which is crew-shaped.
Should the existing topologies be reclassified as crews, or does "swarm" in the code keep a broader
meaning than in the org model?

> **Answer:**


### ~~Q-28~~ — Crews vs swarms? **ANSWERED — COMPLETELY SEPARATE**
**Blocks:** UI-049, UI-050, UI-051 · **Status:** closed 2026-08-23 · **Detail:** UI-104

> **Steven:** *"Crews are considered employees. You'll have managers, administrators, coders, CEO,
> CFO, CTO. Treat it like a business or a military or a guild. It is an organization piece.
> Completely separate from swarms. Swarms are many agents doing the same exact job. Crews are many
> jobs, many agents."*

Clean distinction: **swarm = homogeneous parallelism; crew = heterogeneous organisation.** My
earlier suggestion that crews be a front-end onto topologies is withdrawn.

**One thing to resolve first (Q-64):** the existing topologies are *heterogeneous* —
`planner -> coder -> tester -> reviewer -> documenter` is many jobs, not one job repeated. By this
definition they are crew-shaped, so "swarm" in the code does not currently mean "swarm" in the
model.

### ~~Q-29~~ — Does a boss route work, or merely own subordinates? **ANSWERED — BOTH**
**Blocks:** UI-049 · **Status:** closed 2026-08-23

> **Steven:** *"A code worker crew member will need to report to their boss, who may be the Game
> Architect, who reports to their boss who may be the manager. There could be 200 crew members
> broken into teams of 5, and each team will report to their respective department."*

A real org chart, not a single boss/worker tier:

- **arbitrary depth** — worker -> architect -> manager -> ...
- **teams** as a grouping unit (~5)
- **departments** above teams
- reporting is both ownership *and* work routing upward

Schema needs `parent_id`, plus team and department grouping. None exist today.

### ~~Q-43~~ — `settings.runtime`: remove, view, or keep anonymous? **ANSWERED — VIEW**
**Blocks:** UI-075, UI-076 · **Status:** closed 2026-08-23

> **Steven:** *"We do not know what is available to you until you have made the contact. Then can we
> view the resource. This suggests making a view onto."*

Becomes a **view onto contacts that have granted node access** — resources are discoverable only
after a contact exists. `runtime_instances` and its anonymous URL + token model go away.

### ~~Q-14~~ — `python/ledger/` vs `blockchain/src/app.js`? **ANSWERED — DELETE THE PYTHON ONE**
**Blocks:** UI-027 · **Status:** closed 2026-08-23

> **Steven:** *"Are you sure it's not the second blockchain? It seems like the other works better.
> So delete."*

**Verified — your read is right.** The Node server is both live and strictly more capable:

| | `blockchain/src/app.js` (1008 lines) | `python/ledger/server.py` |
|---|---|---|
| endpoints | `/chain/status`, `/chain/blocks`, `/chain/tx`, `/chain/mint`, `/chain/peers`, `/chain/peers/register`, `/chain/peers/heartbeat`, `/chain/reconcile` | `/chain/tx`, `/chain/import` only |
| UI talks to it | **yes** — `renderer.js` fetches `/chain/status` and `/chain/blocks` | no |
| imported by production code | yes | **no** — only a commented-out line |

`python/ledger/` is a strict subset with no status, blocks, peers or reconcile. Safe to delete; its
only live reference is a passing test.

### ~~Q-11~~ — Ghost vs Real: one chain or two? **ANSWERED — TWO, SEPARATE**
**Blocks:** UI-023, UI-024 · **Status:** closed 2026-08-23

> **Steven:** *"The Ghost chain is only viewable locally. The Real chain is viewable by all on the
> chain. They are meant to be separate."*

Two chains, distinguished by **audience**: Ghost is local-only, Real is shared with everyone on it.
Not one chain with tagged transactions — so the "add a mode column" approach from my earlier note is
off the table.

**Useful discovery:** the Node chain already exposes **`/chain/reconcile`**, plus a full peer
directory (`/chain/peers`, `/register`, `/heartbeat`). The primitives for a shared chain with peers
exist; what is missing is the *second* chain and the split between them.

### ~~Q-12~~ — What belongs on the Real Chain? **ANSWERED**
**Blocks:** UI-024 · **Status:** closed 2026-08-23

> **Steven:** *"If the TALA harness never goes online, and never connects to any other service, then
> the Real chain never gets used. If you connect with other users, use other skills, transact with
> AUTH or online storage, it gets recorded by the Real chain."*

**Rule: the Real chain records external interactions only.** Peer contact, using another party's
skills, AUTH transactions, online storage. A fully offline install never writes to it.

Confirms the current 2,713 `node-create` transactions are all Ghost by definition — local document
activity, no external party involved.

### ~~Q-35~~ — Should guardrails absorb contacts-permissions and firewall? **ANSWERED — YES**
**Blocks:** UI-061, UI-062 · **Status:** closed 2026-08-23

> **Steven:** *"Yes absorb"*

One policy object replaces three unconnected systems. Combined with Q-36 and Q-37 this fully
specifies its shape — see **The policy object** above.

### ~~Q-36~~ — What is a policy's subject? **ANSWERED — PAIR**
**Blocks:** UI-062 · **Status:** closed 2026-08-23

> **Steven:** *"Pair"*

Subject is **(contact, agent)** — this person, acting through this agent. Combined with Q-22's
per-resource grants, a policy is *(contact, agent) -> resource -> constraints*.

### ~~Q-37~~ — Ports: outbound or inbound? **ANSWERED — BOTH**
**Blocks:** UI-063 · **Status:** closed 2026-08-23

> **Steven:** *"Both"*

Policies constrain both outbound calls Tala makes and inbound ports a peer may reach. Neither exists
today — no port concept appears anywhere in the codebase.

---

## P3 — Capability gaps: this tier is what "so the functionality is there" means

**All twelve answered 2026-08-23.** One follow-up: **Q-65**. Two answers widen scope rather than
close it — Q-33 (the tool registry is not HTML-NN backed) and Q-40 (no storage client exists for any
backend).

### Q-65 — Vault search vs the network-isolation guard
**Blocks:** UI-038 · **Who:** Steven · **Cost:** low · *from Q-20*

`vault:read` is refused unless network isolation is active (UI-102), but Unified Search's `web`
scope needs the network. A search covering both the vault and the web is therefore contradictory
under the current guard. Does vault search drop the web scope, does the guard relax for search, or
do the two scopes become mutually exclusive in the UI?

> **Answer:**


### ~~Q-56~~ — Per-OS path table for MCP discovery? **ANSWERED — YES**
**Blocks:** UI-097 · **Status:** closed 2026-08-23

> **Steven:** *"per-OS path table is wanted."*

Replaces the `%VAR%` strings with a real per-platform table. Claude Desktop lives at
`~/Library/Application Support/Claude/` on macOS and `~/.config/Claude/` on Linux; the two
backslash-separated project paths need POSIX separators too.

### ~~Q-34~~ — `workflow.execute` tool, or generic RPC-invoke? **ANSWERED**
**Blocks:** UI-060 · **Status:** closed 2026-08-23 · **Detail:** UI-106

> **Steven:** *"A workflow is a series of tool calls, that pass one bit of info from one tool to the
> next until desired outcome. Tala has the ability to call an html-nn and act upon that. Or at least
> she should."*

Neither option as posed — the mechanism is **acting on an HTML-NN**, since workflows compile to one.
No generic RPC-invoke tool needed.

**Half of it already exists.** She has `read_neural_node`, `list_neural_nodes` and
`update_neural_node`, so she can *call* a node. What is missing is *acting on* it: there is no
execute path, so reading a compiled workflow node tells her what to do and gives her no way to do
it. That is the precise gap.

### ~~Q-50~~ — Which log surface does Tala get? **ANSWERED — READ-ONLY**
**Blocks:** UI-086 · **Status:** closed 2026-08-23

> **Steven:** *"She cannot clear her logs but I should be able too."*

Tala gets `logging.get_events` and `logging.get_health` as tools. `archive_events` and
`clear_events` stay UI-only, operator-invoked. She can diagnose herself but cannot erase her own
audit trail.

### ~~Q-49~~ — Bridge `logger.*` into `agent_log`, or convert call sites? **ANSWERED — BRIDGE**
**Blocks:** UI-085 · **Status:** closed 2026-08-23

> **Steven:** *"Bridge"*

A logging handler writing through `log_to_db` catches all 257 existing call sites and every future
one, rather than converting them individually and missing new ones.

### ~~Q-7~~ — Must Tala's browsing be visible? **ANSWERED — PREFERRED, NOT REQUIRED**
**Blocks:** UI-013 · **Status:** closed 2026-08-23

> **Steven:** *"I like to watch her navigate but if its to complex"*

Visibility is a preference with an explicit escape hatch, not a hard requirement. That makes UI-013
a **UX gap rather than a defect** — and it points directly at the recommendation under Q-6.

### ~~Q-6~~ — Re-point browser tools, or mirror? **ANSWERED — MY CALL: MIRROR**
**Blocks:** UI-013, UI-016 · **Status:** closed 2026-08-23

> **Steven:** *"Which ever would be more profitable."*

Delegated, so here is the recommendation and the reasoning: **keep Playwright as the driver and
mirror its navigation into the visible webview.**

| | Re-point tools at the webview | Mirror Playwright into the webview |
|---|---|---|
| capability | Electron `<webview>` lacks Playwright's wait primitives, network interception and robust selectors | keeps all six working tools unchanged |
| coupling | agent work would require the UI open and on that tab | agent work stays headless and independent |
| build cost | rewrite all six tools against a weaker API | **mostly already built** — `browser.navigate` push works today, listener at [renderer.js:7147](../renderer/renderer.js#L7147) |
| satisfies Q-7 | yes | yes |

Mirroring gives the watchability Steven wants at a fraction of the cost, and does not trade away
Playwright's reliability. Re-pointing would be the more expensive option *and* the weaker one.

### ~~Q-19~~ — Does "local LLM" mean Ollama-only? **ANSWERED — ANY LOCAL ENGINE**
**Blocks:** UI-038 · **Status:** closed 2026-08-23

> **Steven:** *"Ollama, vllm, llama.cpp, any one the end user decides to install."*

The vault gate keys on **provider type**, not vendor — `local` and `embedded` qualify, `remote` does
not. Matches the Engine Manager, which already offers HuggingFace, vLLM and llama.cpp alongside
Ollama.

### ~~Q-18~~ — Vault scope: files or HTML-NN? **ANSWERED — ANYTHING**
**Blocks:** UI-036, UI-037 · **Status:** closed 2026-08-23

> **Steven:** *"This could be anything as it is being privately added and we want source of truth."*

Arbitrary files. **This is a deliberate exception to the HTML-NN universal-data mandate** — worth
recording as such, since that mandate otherwise governs everything the harness creates or consumes.
The reasoning is source-of-truth: a privately added document should be stored as it is, not
transformed.

### ~~Q-20~~ — Vault as a Unified Search source? **ANSWERED — YES**
**Blocks:** UI-038 · **Status:** closed 2026-08-23

> **Steven:** *"If its in the vault yes"*

**One conflict to resolve (Q-65).** `vault:read` is refused unless network isolation is active, but
Unified Search's `web` scope requires network. So a single search cannot cover both the vault and
the web under the current guard.

### ~~Q-31~~ — Profile in every prompt, or on demand? **ANSWERED — ON DEMAND**
**Blocks:** UI-057 · **Status:** closed 2026-08-23

> **Steven:** *"Tala needs to reference it only when she determines its necessary."*

Agent-initiated retrieval, not prompt injection — so this is a **tool**, not a system-prompt change.
Costs no tokens on turns that do not need it.

### ~~Q-33~~ — Workflow palette from tools or HTML-NN? **ANSWERED**
**Blocks:** UI-059 · **Status:** closed 2026-08-23 · **Detail:** UI-107

> **Steven:** *"The tool registry should be a collection of html-nn. If they are not we need to
> address that."*

**They are not.** Checked: 66 tools are registered as Python functions via `@register_tool`, and the
56 `sdk_agent_*.html` nodes document **modules** (`sdk_agent_planner`, `sdk_agent_shell`, ...), not
individual tools. There is no node per tool and no correspondence between the two counts.

So the palette question resolves into a larger one: making the registry itself HTML-NN-backed, per
the universal-data mandate. Recorded as UI-107.

### ~~Q-40~~ — Which storage backends first? **ANSWERED — ANY**
**Blocks:** UI-068 · **Status:** closed 2026-08-23

> **Steven:** *"We don't add these, user adds these. So it can be any storage backend."*

The user configures the backend, so the panel stays generic — its nine types plus `custom` already
allow that.

**Worth being explicit:** the user supplying an endpoint and credentials does not remove the need
for a **client library** in the app. None of `boto3`, `paramiko`, `googleapiclient`, `msal` or
`azure-storage-blob` is present or declared, so no configured backend can currently be reached
regardless of who configures it. UI-068 stands.

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

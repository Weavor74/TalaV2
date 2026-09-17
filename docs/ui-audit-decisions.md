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

**P1 is complete**, including all follow-ups. Q-63 closed the model: agents are principals with
assigned privileges, just as peers are (UI-118). **Deferred:** Q-60 (classification taxonomy), at
Steven's request.

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

### ~~Q-67~~ — What does "edit files and documents" cover? **ANSWERED — ALL**
**Blocks:** UI-114 · **Status:** closed 2026-08-23

> **Steven:** *"They should cover all, image and text."*

Both image and text, no format carve-outs. Against what exists — a `<textarea>` with preview gated
to `.md` and `.html` — that is a substantial build, not an adjustment:

| Need | Today |
|---|---|
| text | works |
| Markdown / HTML with preview | works |
| **code with language awareness** | plain text only |
| **images** | **impossible** — a `<textarea>` cannot hold one |

Images in particular need a different editing surface entirely, not an extension of the current one.

### ~~Q-63~~ — Which principal does `AuthorizationGate` resolve against? **ANSWERED**
**Blocks:** UI-081, UI-103 · **Status:** closed 2026-08-23 · **Detail:** UI-118

> **Steven:** *"Ah so in short administrative privileges. For local actions by agent model. An
> agent, should be assigned just like a peer."*

**Agents are principals too, assigned privileges the same way peers are.** That is the missing
piece — the gate does not need a special case for locally-initiated work, because the *agent* is
always the actor regardless of who asked.

Three principals, one mechanism:

| Principal | Privileges |
|---|---|
| **Owner** (you) | administrative — implicit, unrestricted |
| **Agent** | assigned, exactly as a peer's are |
| **Peer** (contact) | assigned per resource, by invitation (Q-22) |

Resolution:

- **You ask Tala to do something** → the acting agent's privileges govern it. Your administrative
  standing does not bypass them; it is what lets you *assign* them.
- **A peer's request triggers work** → the **(contact, agent) pair** from Q-36, both halves
  consulted. Neither can exceed its own grant.

**Schema consequence:** `agents` currently holds `id, name, type, role, crew_type, is_active,
config` — **no privileges dimension at all**. It needs the same grant structure as contacts.

---

## P2 — Architecture forks: answering late means building the wrong thing

**All nine answered 2026-08-23.** Two of my recommendations were overturned: crews are *not* a
front-end onto topologies (Q-28), and Ghost/Real are *not* one chain with tagged transactions
(Q-11). One follow-up: **Q-64**.

### The policy object

Q-35 (absorb), Q-36 (pair) and Q-37 (both) combine with Q-22 (per-resource) into one shape:

> **(contact, agent) -> resource -> constraints**

**Completed by Q-63.** Both halves of the subject are *principals with assigned privileges* —
agents are assigned just like peers. The owner sits above both with administrative standing, which
is what confers the right to assign. A locally-initiated action still resolves against the acting
agent's privileges; only the peer half drops out.

*"This person, acting through this agent, may reach this resource, on these ports, within this
window."* — exactly the example Steven opened the guardrails panel with. Constraints so far:
**ports** (inbound and outbound, Q-37) and a **time window** (the original example; `leases.py`
already has TTL machinery).

Precedence from Q-32: **lockdown -> contact grant -> profile visibility**.

### ~~Q-64~~ — Are the existing "swarm" topologies actually crews? **ANSWERED**
**Blocks:** UI-049, UI-051 · **Status:** closed 2026-08-23

> **Steven:** *"They can be a member of the crew but not the crew in completeness."*

Resolves the naming conflict cleanly: **a swarm is a unit that can sit inside a crew, not a crew
itself.**

So a crew's members are not all individual agents — a member can be a single agent *or* a swarm
acting as one working unit. `Code Repair Swarm` is a legitimate crew member; it is not a crew.

That keeps both concepts intact and gives the org model a composition rule: crew contains members;
a member is an agent or a swarm.


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

### ~~Q-65~~ — Vault search vs the isolation guard **ANSWERED — NO NEW SEARCH NEEDED**
**Blocks:** UI-038 · **Status:** closed 2026-08-23

> **Steven:** *"Should we build our own local search?"*

**Recommendation: no — you already have two local search engines, and the pattern that solves this
is already in the codebase.**

| Engine | What it does | Where |
|---|---|---|
| PostgreSQL full-text | `tsvector` over `memory_index` | `local_provider.py` |
| ChromaDB | semantic / vector | `retrieval.py` |

The conflict is not a missing search engine. It is that `vault:read` refuses unless network
isolation is on, while Unified Search's `web` scope needs the network — so one query cannot use both.

**The fix already exists as a proven pattern: PII.** Profile PII is indexed into `memory_index`
tagged `trust_label='private'`, and the search SQL filters it out unless explicitly asked for:

```sql
AND (trust_label != 'private' OR $4::boolean = TRUE)
```

Vault content can ride exactly that path — indexed at write time, searchable through the existing
engines, with the file itself still read through the guarded `vault:read`. Search returns *that a
match exists*; opening the document still respects isolation.

Four independent guards already protect PII this way (D11). Reusing them beats building a third
search engine.


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

**Sixteen of eighteen answered 2026-08-23.** Two are **re-asked below** — Q-5 and Q-38 — because my
original wording was unclear, not because Steven declined them.

Three answers changed the shape of a finding rather than just closing it: **Q-25** (the env buttons
are real presets), **Q-42** (multi-DB is content partitioning, not failover) and **Q-41** (storage is
for HTML-NN distribution, not backup). One follow-up: **Q-66**.

### ~~Q-66~~ — What are the Git providers for? **ANSWERED**
**Blocks:** UI-067 · **Status:** closed 2026-08-23

> **Steven:** *"Tala info should come from the website but it doesn't mean that other content and
> code projects don't come from git. Git provides a good local repo for various things."*

Two separate uses, and Q-39 only settled one of them:

| Use | Mechanism |
|---|---|
| **Tala's own updates** | the website — **not** git |
| **User code and content projects** | git, via the configured providers |

**This qualifies Q-39.** Clone is not needed for self-update, but it plausibly *is* needed for
working with user repos — so UI-067's missing clone/push is not simply off the table. What is
confirmed wrong is `system.trigger_update` running a bare `git pull` to update Tala herself.


### ~~Q-2~~ — The green dot beside **AGENT CHAT**? **ANSWERED**
**Blocks:** landing node · **Status:** closed 2026-08-23

> **Steven:** *"Active agent"*

Indicates an agent is active. Steven's wording for the map: **"Active agent"**.

### ~~Q-5~~ — What do the two pills on each Neural Node card mean? **ANSWERED**
**Blocks:** landing node · **Status:** closed 2026-08-23

> **Steven:** *"Authority weights"*

Steven's term for the map: **authority weights**. `Exec:` is execution authority, `Mem:` is memory
authority — but "authority weights" is what the node should say when asked.

### ~~Q-1~~ — Status-bar token field? **ANSWERED — WALLET BALANCE**
**Blocks:** UI-001, UI-002, UI-003 · **Status:** closed 2026-08-23

> **Steven:** *"Wallet Balance"*

Confirms UI-001 option (a): re-point `harness.health` at `get_balance()` instead of summing
`token_ledger`. Also makes the dead `Ghost Tokens` infinity branch reachable (UI-002) and requires
the sidebar bar's hardcoded 1,000,000 max to be reconsidered (UI-003).

### ~~Q-3~~ — `MOOD` / `MD` / `JSON`? **ANSWERED — DELETE**
**Blocks:** UI-004, UI-005, UI-006 · **Status:** closed 2026-08-23

> **Steven:** *"Delete"*

**Done 2026-08-23.** All three buttons removed from index.html, and the catch-all handler removed
with them so a future chat-action button cannot silently inherit the same toggle-and-log stub. Worth also fixing the delegated handler at
[renderer.js:4894](../renderer/renderer.js#L4894) that gave them their toggle-and-log stub, so a
future `.chat-action-btn` does not silently inherit the same no-op.

### ~~Q-17~~ — Retention for finished tasks? **ANSWERED — ARCHIVE ON DONE + MANUAL**
**Blocks:** UI-034 · **Status:** closed 2026-08-23

> **Steven:** *"Archive when done. But needs to also be manual."*

Automatic archive on completion, plus a manual archive control. Also relieves UI-030 — the 50-row
cap stops being a practical ceiling once completed tasks leave the active list.

### ~~Q-24~~ — `APPLY CHANGES`? **ANSWERED — STAGED-THEN-APPLY**
**Blocks:** UI-045 · **Status:** closed 2026-08-23

> **Steven:** *"Staged-then-apply"*

The button becomes real: panels stage edits, `APPLY CHANGES` commits them. That is the larger of the
two options — every panel currently saves on change, so each needs a staging buffer.

### ~~Q-25~~ — `USB` / `LOCAL` / `REMOTE`: what should each do? **ANSWERED**
**Blocks:** UI-044, UI-039 · **Status:** closed 2026-08-23 · **Detail:** UI-109

> **Steven:** *"The way it was supposed to work is if you select the USB button you are running from
> a USB and all data stored is on USB. The embedded engine is all we have access to. Think of it as a
> troubleshooting mode. If you selected local then you are using local non-usb storage, inference
> engine such as ollama or lmstudio. And cloud means everything may be remote... It's also okay to
> think of it as presets."*

**Presets**, each setting data root + permitted inference + permitted storage together:

| Preset | Data root | Inference | Storage |
|---|---|---|---|
| **USB (ISOLATED)** | the USB itself | **embedded engine only** | USB only |
| **LOCAL (SYSTEM)** | local non-USB | local engines — Ollama, LM Studio, llama.cpp | local |
| **REMOTE (CLOUD)** | may be remote | any, including cloud APIs | any, including cloud |

USB doubles as a **troubleshooting mode** — the narrowest possible configuration.

Note the tiers are stricter than lockdown: lockdown (Q-62) permits any *local* engine, USB permits
only the *embedded* one.

### ~~Q-26~~ — `GLOBAL` vs `WORKSPACE` scope? **ANSWERED — DELETE**
**Blocks:** UI-044 · **Status:** closed 2026-08-23

> **Steven:** *"Leftover chrome"*

Remove the selector. Unlike the env presets beside it, this one has no intended meaning.

### ~~Q-27~~ — Embedding-only providers? **ANSWERED — OWN SECTION**
**Blocks:** UI-048 · **Status:** closed 2026-08-23

> **Steven:** *"Move to own section"*

They leave the inference priority list entirely, so the displayed order finally matches the
effective fallback chain.

### ~~Q-38~~ — Should the search provider `ACTIVE` badge be real? **ANSWERED — MAKE IT REAL**
**Blocks:** UI-064 · **Status:** closed 2026-08-23

> **Steven:** *"Make it real."*

A cheap authenticated request per provider, so a dead or expired key shows red. Matters because
nothing else in the app would reveal a failed provider — UI-018 has it falling back silently to
DuckDuckGo scraping.

### ~~Q-53~~ — Nine hardcoded subsystem statuses? **ANSWERED — WIRE THEM ALL**
**Blocks:** UI-091 · **Status:** closed 2026-08-23

> **Steven:** *"Wire it all"*

All twelve indicators get real probes. Three already do (`DB_HEALTH_SERVICE`, `INFERENCE_SERVICE`,
`POLICY_GATE`); the other nine are string literals today, including both that read `DEGRADED`.

### ~~Q-54~~ — Is `mem0` a real dependency? **ANSWERED — REMOVE**
**Blocks:** UI-093 · **Status:** closed 2026-08-23

> **Steven:** *"If it's not installed remove"*

It is not installed — an optional extra in `pyproject.toml`, and `import mem0` fails in the active
venv. "Dual Memory: mem0 + RAG" drops to the RAG half, which is real via ChromaDB.

### ~~Q-51~~ — Architecture diagram: aspirational or descriptive? **ANSWERED — REALITY**
**Blocks:** UI-088 · **Status:** closed 2026-08-23

> **Steven:** *"I want it to reflect reality"*

So the four inaccurate boxes need correcting: `PostgreSQL Vector DB` is not a vector DB, and
Workflow Executor, Blockchain Firewall and P2P Mesh Router each describe capability that is only
partly real.

### ~~Q-52~~ — Add a Filesystem / HTML-NN Node Store box? **ANSWERED — YES**
**Blocks:** UI-089 · **Status:** closed 2026-08-23

> **Steven:** *"Yes"*

The project's defining component gets its own box, and PostgreSQL stops being credited with holding
the nodes.

### ~~Q-39~~ — Do clone/push need building? **ANSWERED — NO**
**Blocks:** UI-067 · **Status:** closed 2026-08-23

> **Steven:** *"Technically she should get her updates from the website and not pull the repo."*

Updates come from a website, not git. So UI-067's missing clone/push are **not** work to schedule —
and `system.trigger_update`'s bare `git pull` is itself the wrong mechanism.

**Leaves open (Q-66):** if updates do not use git, what are the configured Git providers *for*?

### ~~Q-41~~ — Is off-machine backup of `nodes/` required? **ANSWERED — BIGGER THAN BACKUP**
**Blocks:** UI-069, UI-056 · **Status:** closed 2026-08-23

> **Steven:** *"We are meant to be able to store html-nn anywhere, so we can share or remotely
> access."*

**Corrected 2026-08-23.** Steven: *"Not required. As well as not only. TALA can store text, image,
audio or anything in the remote storage. A common use may be remote html-nn."*

So remote storage is **optional, and general-purpose** — any content type, with HTML-NN as one
common use rather than the purpose. My earlier framing here ("for HTML-NN distribution, not
backup") over-narrowed it *and* over-elevated it. UI-068 stands as a real gap, but as a
**capability that is unavailable**, not a blocked requirement.

### ~~Q-42~~ — Multi-database routing? **ANSWERED — PARTITION BY DOMAIN, NOT FAILOVER**
**Blocks:** UI-071, UI-072 · **Status:** closed 2026-08-23

> **Steven:** *"The multi database is used for things like, I store all my info on coding on this DB
> and all my info on conversation on this DB."*

**Not replication or failover — content routing.** Each database holds a subject domain.

**Steven, confirming 2026-08-23:** *"Yes they technically are not failovers as you can't replicate
data across multiple types of DB. They are additional DB."* — additional databases, not replicas.

That means the panel's stated rules are describing the wrong feature entirely: *"Automatic Failover
— if the primary replica goes offline, reroute to the next healthy standby"* has nothing to do with
partitioning by subject. UI-072's finding stands but its framing needs replacing rather than
implementing.

### ~~Q-13~~ — Is `talacore.app` the Real Chain host? **ANSWERED — YES, NOT YET CONFIGURED**
**Blocks:** UI-025 · **Status:** closed 2026-08-23

> **Steven:** *"It is, but has not been fully configured yet."*

Confirms the CSP allow-list entry was deliberate. The renderer's hardcoded `127.0.0.1:8332` will
need to become configurable before it can reach it.

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


---

## Highs pass notes (2026-09-17)

### H2 — :8765 authentication
`python/harness/ws_auth.py` is the Wave 0+ admission gate (Origin + Bearer token).
Docs that still describe an unauthenticated local JSON-RPC socket are stale; prefer
this module over historical audit prose.

### H6 — Resume ≠ RELEASE HALT
Verified: Mission Control Resume uses `task.control` resume only;
`mcReleaseEmergencyHalt` alone calls `system.release_halt`. No further code change.

### H7 — Fail-open defaults retained
Lease mode defaults to `open` (`TALA_LEASE_MODE`); network guard defaults to
`enabled: false` / `default: allow`. Tightening would break intentional opt-in
tests. Documented as unsafe defaults; Settings UI should warn when guard is off /
lease is not `enforce`.

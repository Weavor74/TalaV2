# Node learning and the nightly dream

HTML-NN node weights now change with use and feedback, and a consolidation pass (the dream) runs on a schedule. Before
this, retrieval ranked by `similarity x weight_relevance x weight_confidence` but nothing ever wrote those weights back.
The older helpers edited a `node-metadata` JSON block that 358 of 361 real nodes do not have and retrieval never reads.

Code: `sdk/agent/node_learning.py` (the rules), `sdk/agent/dreaming.py` (the dream), `sdk/agent/memory_tools.py`
(`write_memory`), `python/harness/learning_rpc.py` (RPCs), `renderer/learning_feedback.js` (controls).

## What changes, and where

The attributes on a node's `<article>` tag, edited in place:

| attribute | meaning | moved by |
|---|---|---|
| `data-weight-relevance` | worth recalling | use (up), idle decay (down) |
| `data-weight-confidence` | how far to trust it | feedback only |
| `data-usage-count` | turns the node was injected into | use |
| `data-last-used` / `data-last-decay` | clocks for the decay | use / decay |

Everything is clamped to [0.05, 1.0]. The rest of the node file is preserved byte for byte.

| event | step | bound |
|---|---|---|
| node injected into a turn | relevance +0.02 | at most +0.10 per node per UTC day; the use is always counted |
| thumbs up | confidence +0.05 | |
| thumbs down | confidence -0.10 | |
| "that's wrong" | confidence -0.20 | |
| idle for 7 days after a use | relevance -0.01 per idle period | floor 0.50 |

A node nothing has used has no usage attributes, so time alone never erodes it. Protected nodes (governance importance
0.90 or more, types `core` / `safety` / `vault`, ids starting `persona-`) are never changed by learning.

## Saving what the user tells her

`write_memory` creates a real learning node (`fact-...`), starting at 0.80 / 0.80 so use and feedback have room to move
it. The same fact twice is the same node, left untouched. Credentials and secrets are refused. A message that explicitly
says "remember that ..." is saved by the server before retrieval runs, so the same turn can find it.

## The dream

`dream_if_due` rides the heartbeat tick. It runs once a day after `dream_hour` (local, default 3), when the system has
been idle, learning is on and the emergency halt is not engaged. A dream:

1. snapshots every node file;
2. applies the idle decay;
3. runs `GoalEngine.dream_cycle`;
4. checks the result: no protected node changed, no node learning had never touched changed, every weight is in bounds,
   nothing was deleted;
5. on any exception or failed check, **restores every file from the snapshot** and records a rollback;
6. writes a `dream-journal-YYYY-MM-DD` node (weight 0.30, so a journal never outranks real memory).

Today is marked as attempted whether or not the dream rolled back, so a failing dream is not retried every minute.

## Switches and safety

* **Off**: Settings > LEARNING, `learning.set_enabled`, or `TALA_LEARNING=0` (which wins over the saved setting).
  Off or halted means no node is read for learning and none is written. A halt that cannot be read counts as halted.
* **Reset weights**: Settings > RESET WEIGHTS, or the `reset_node_weights` tool. It needs `confirm=true`, runs through
  `ToolRegistry.run_tool` (so the AuthorizationGate, the lease policy and the risk classifier all apply), and works while
  learning is off but never while halted.
* **Halt gate**: no `learning.*` method is on the halt allow-list, so an emergency halt refuses all of them.
* **Ledger**: every weight change is logged to `<user data>/learning/events.jsonl` and anchored in one ledger
  transaction per batch (`node-weight-update`, a local kind: it never goes to the shared chain). The transaction carries
  node ids, field names and values only, never key material.
* Settings live in `<user data>/learning.json`: `enabled`, `dream_enabled`, `dream_hour`.

## RPCs

| method | params | does |
|---|---|---|
| `learning.feedback` | `node_ids` (1-8 ids), `kind` up/down/wrong | moves confidence of those nodes |
| `learning.status` | | setting, halt state, nodes touched, recent log |
| `learning.set_enabled` | `enabled` (bool) | persists the toggle |
| `learning.dream_now` | | runs the dream now (skips the schedule, never the halt or the off switch) |
| `learning.reset_weights` | `confirm: true`, `node_ids?` | admin reset through the gated tool registry |

`inference.generate` replies now carry `injected_node_ids` (the nodes the reply was built from, which the thumbs act on)
and `remembered_node_id`.

## One write path

`adjust_behavioral_weights` delegates to `node_learning.apply_text_feedback` for any node carrying the weight
attributes. Nodes without them (the `node-metadata` JSON era) still use the legacy path.

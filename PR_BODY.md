# Stop reporting success over silent failure

47 commits. The branch began as one narrow fix — paths that turned failure into apparent success — and the theme kept holding as more of the system was checked. Almost every subsystem examined had a place where "it didn't happen" was reported as "fine".

**Gate:** `pytest` → **592 passed**, ~100s. It was 41 minutes and 395 tests at the start of the branch. 11 live-model tests are marked `llm` and deselected by default; `conftest.py` prints how many were held back and how to run them.

**On the diff size.** 417 files and ~59k insertions, but that is not 417 files of logic. **242 are generated corpus stories** and 106 are HTML-NN nodes (mostly regenerated documentation); `uv.lock` accounts for another 3,360 lines. The hand-written change is **6 files in `python/harness/`, 19 in `sdk/agent/`, 2 in `renderer/`, 10 scripts and 23 test files.** Start there.

---

## The through-line

Every one of these announced work that never occurred:

| Subsystem | What it claimed | What was happening |
|---|---|---|
| Checkpoints | rollback succeeded | `create_checkpoint` returned the string `"failed"` as a checkpoint name; read-only files defeated `rmtree(ignore_errors=True)` |
| Synthesis | phase complete | reused a stale `scratch/output.py` after a timeout |
| Terminal | command ran | the bash backend ran nothing and reported success |
| Heartbeats | job `running` | status was never written when the process died; one row read `running` for 16 hours |
| LoRA training | "Successfully trained LoRA" | wrote 51 bytes of ASCII, or executed a bare `pass` |
| Doc generation | node updated | rewrote an identical node, differing only by a timestamp it had just stamped |
| OTA updates | update available | `git pull`'s exit code was never checked; a failed pull still told every client to download |
| Task pause | paused | wrote `blocked`, which is what auto-resume hunts for |

Each is now an error, a warning naming the thing that failed, or a status that reflects reality.

---

## Larger findings

**94 of 115 nodes could never be recalled.** Activation is a pure product and governance acts as a hard gate, so a node with zero authority scores zero at any similarity. Documentation nodes are correctly generated with `execution_authority=0.00`; three generators copied that same zero into `memory_authority`, which does not mean "cannot act" but "cannot be remembered". Retrieval never errored — it answered every question from the ~18% of the graph that had authority, which is mostly settings. Asking *"how do I write an HTML neural node"* returned `settings-code-registry` at 0.26 similarity while `system-html-nn-authoring-guide`, at 0.43, scored exactly zero.

**The test suite wrote into the running application's database.** Its fixtures insert real task rows, so Mission Control's queue repopulated itself on whatever cadence someone ran `pytest`. Reported as *"I clear the queue and after twelve minutes they all come back."* Now redirected to a sibling database before anything imports settings.

**Small local models could not call tools they had documentation for.** The harness dispatches on a text `<call:tool />` convention, which asks a model to reproduce an exact tag from prose. The 4B that 6GB of VRAM allows cannot; it narrates instead. Native function-calling now goes to every provider that accepts it, converted back to `<call:/>` at the boundary so nothing downstream changed.

**Tools cost 5,975 prompt tokens on every turn**, on a box whose ceiling is 16k and whose real prompts reach 11k. Per-turn keyword selection cuts that to ~1,116 — 81% — while keeping full argument schemas, which matter because making argument names visible is what took the agent benchmarks from 2/6 to 4/6.

---

## What was added, not just fixed

- **Mission Control** gains schedule management (add / edit / pause / run / delete) with a cron *builder*, and task queue management (add / edit / delete / clear). The cron scheduler had run from boot since it was written with no UI ever calling its six RPC methods.
- **LoRA training exists now.** `scripts/train_voice_adapter.py` trains on the story corpus; the first real adapter is 2,175,168 bytes against the 51-byte placeholders it replaced. Aimed at voice and behaviour, not knowledge — retrieval is better at facts at every scale.
- **242 training stories**, up from 5, generated unattended by the nightly job once the scheduler was repaired.
- **Self-knowledge documentation** — nodes describing activation, weights, adapter training and Ollama deployment, because the AST-derived docs carry structure and no behaviour, which is what made her confabulate mechanism.
- **Swarm topologies as HTML-NN** (@Weavor74's work, committed as found and passing): topologies load from `nodes/swarm-topology-*.html` with a UI editor, and gate conditions are generated from `GatedVerifier.KNOWN_RULES` rather than hardcoded — an editor offering a rule the verifier does not implement stalls that branch silently on every run.

---

## Review notes

**Not one theme.** The branch name undersells it. If you would rather review in pieces, the natural seams are: inference and tool calling · memory and self-knowledge · Mission Control UI · LoRA and corpus · test infrastructure.

**Authored-and-unreviewed.** `era_availability` in the emotion vocabulary node was destroyed by the 2026-08-11 rollback and could not be recovered — never committed, and no pre-loss story survives to mine. It was re-authored, and is now derived from a stated rule (`scripts/derive_era_availability.py`) so the entire invented surface is four exclusions on one screen rather than four opaque lists. The measured cost of each exclusion is in the commit. **This wants a human decision.**

**Known and unclosed:**
- Messaging channels remain at 1 adapter (Telegram) against OpenClaw's 25+. Manager, schema and hot-reload all work, so each platform is additive.
- Swarm and P2P are still unverified end to end — `p2p_sessions` is empty and no test exercises a fleet task.
- The trained adapter cannot reach the model that serves chat: it is fitted to Qwen2.5-0.5B, chat runs the 4B under Ollama, and adapter and base must match. Ollama also cannot swap adapters per request. The procedure is documented in `nodes/system-ollama-adapter-deployment.html`; nobody has walked it.
- `torch` installed as `2.13.0+cpu`, so training leaves the RTX 3050 idle.
- Six `python/tala_engine.egg-info/*` files are tracked but are build output; they should be ignored and untracked.
- Three zero-byte files named after tools (`research.search,`, `research.crawl_and_summarize`, `no`) were deleted, but the code path that creates them was not found. They will return if it is still live.

**Worth knowing while reviewing:** a junk skill file, `sdk/agent/skills/word_reverser.py`, contained a module-level `print` and no tool decorator. `auto_discover_skills()` executes every skill file to find decorators, so it wrote `15` to stdout on every tool-registry load — including processes whose output is parsed. Removed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

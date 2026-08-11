"""
sdk/agent/self_documenter.py
===========================
Self-Documentation Pass — Tala Cognitive Engine Room
Milestone 10 | Tasks 10.1, 10.2

Provides two documentation paths:

  self_document_phase()
      Per-phase development documentation node (phase_documentation type).
      Called manually during milestone work.

  document_completed_goal()                          ← NEW (Task 10.2)
      Auto-skill extraction node (skill type).
      Called automatically by GoalEngine after every successful multi-step
      goal (≥ 2 phases).  Deterministic node id prevents duplicates — the
      same goal re-run updates the existing node rather than creating a new
      one.  The node is machine-readable via the JSON state block so
      activation.py can surface it as working memory context.
"""

from __future__ import annotations
import json as _json
import hashlib as _hashlib
import logging
import os
from datetime import datetime, UTC
from pathlib import Path

from sdk.agent.wiki import WikiUpdater
from sdk.agent.ipc_registry import IPCRegistry
from sdk.agent.scanner import RepositoryScanner
from sdk.agent.anchor import canonicalize_and_anchor
from sdk.agent.documentation_nodes import create_learning_documentation_node

log = logging.getLogger(__name__)


class SelfDocumenter:
    """
    Auto-generates and updates HTML Neural Wiki Nodes documenting completed phases,
    active IPC routes, modified files, and test status.
    """

    def __init__(self, root_dir: str | None = None) -> None:
        self.root_dir = root_dir or os.getcwd()
        self.updater = WikiUpdater(backup=False)

    def self_document_phase(
        self,
        phase_name: str,
        accomplishments: list[str],
        changed_files: list[str],
        tests_status: str = "All passed"
    ) -> str:
        """
        Generates/updates a wiki node representing the completed phase.
        Returns the generated/updated file path.
        """
        # Let's normalize phase name to a slug
        slug = phase_name.lower().replace(" ", "_").replace(":", "").replace("-", "_")
        node_id = f"phase_{slug}"
        node_filename = f"{node_id}.html"

        # Let's locate or create 'nodes' directory in root_dir
        nodes_dir = Path(self.root_dir) / "nodes"
        if not nodes_dir.exists():
            nodes_dir.mkdir(parents=True, exist_ok=True)

        node_path = nodes_dir / node_filename

        # Discover IPC info
        ipc_methods_count = 0
        slash_commands_count = 0
        try:
            registry = IPCRegistry(self.root_dir)
            audit = registry.audit()
            ipc_methods_count = audit.get("total_rpc", 0)
            slash_commands_count = audit.get("total_slash", 0)
        except Exception as e:
            log.warning("SelfDocumenter: IPC audit failed: %s", e)

        # Discover total files in repo
        total_files = 0
        try:
            scanner = RepositoryScanner(self.root_dir)
            total_files = len(scanner.scan_repo())
        except Exception as e:
            log.warning("SelfDocumenter: repository scan failed: %s", e)

        # Build accomplishments list HTML
        acc_html = "".join(f"<li>{a}</li>" for a in accomplishments)
        files_html = "".join(f"<li><code>{f}</code></li>" for f in changed_files)

        timestamp = datetime.now(UTC).isoformat()

        state_data = {
            "phase_name": phase_name,
            "accomplishments": accomplishments,
            "changed_files": changed_files,
            "tests_status": tests_status,
            "metrics": {
                "total_files": total_files,
                "ipc_methods": ipc_methods_count,
                "slash_commands": slash_commands_count
            }
        }
        
        adapter_packet = {
            "activation_keywords": [],
            "tool_affordances": [],
            "forbidden_actions": [],
            "repair_policies": {},
            "validators": {},
            "confidence": 1.0
        }
        
        content_html = f"""
    <h1>{phase_name}</h1>
    <section id="summary">
      <h2>Accomplishments</h2>
      <ul>
        {acc_html}
      </ul>
    </section>
    <section id="changes">
      <h2>Modified Files</h2>
      <ul>
        {files_html}
      </ul>
    </section>
    <section id="telemetry">
      <h2>System Metrics & Telemetry</h2>
      <ul>
        <li><strong>Timestamp:</strong> {timestamp}</li>
        <li><strong>Total Repo Files:</strong> {total_files}</li>
        <li><strong>Active IPC RPC Methods:</strong> {ipc_methods_count}</li>
        <li><strong>Active Slash Commands:</strong> {slash_commands_count}</li>
        <li><strong>Test Status:</strong> {tests_status}</li>
      </ul>
    </section>"""
        
        core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
            node_id, content_html, state_data, adapter_packet
        )
        
        node_provenance = {
            "core_hash": f"sha256:{core_hash}",
            "chaincode_id": chaincode_id,
            "anchoring_status": anchoring_status,
            "raw_response": raw_response,
            "key_fingerprint": public_key if public_key else "unanchored",
            "parent_chaincode_ids": [],
            "created_by": "tala-system",
            "created_at": timestamp,
            "wrapper_hash": "..."
        }
        
        state_json = _json.dumps(state_data, indent=2, ensure_ascii=False)
        prov_json_placeholder = _json.dumps(node_provenance, indent=2)

        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Neural Node: {phase_name}</title>
</head>
<body>
  <article
    data-node-id="{node_id}"
    data-node-type="phase_documentation"
    data-schema-version="neural-node/v2"
    data-execution-authority="0.10"
    data-memory-authority="1.00"
    data-weight-relevance="1.00"
    data-weight-confidence="1.00"
    data-chaincode-id="{chaincode_id}"
    data-content-hash="{core_hash}"
    data-source="local"
    data-trust-tier="system"
    data-privacy="private">
    
    <header id="node-identity">
      <h1 id="node-title">{phase_name}</h1>
    </header>

    <section id="semantic-memory">
{content_html}
    </section>

    <nav id="relationship-anchors" aria-label="Node relationships">
      <h2>Relationships</h2>
      <ul></ul>
    </nav>

    <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
    </script>

    <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{_json.dumps(adapter_packet, indent=2)}
    </script>

    <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
    </script>
  </article>
</body>
</html>
"""
        wrapper_hash = _hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
        node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
        html_content = html_template.replace("__PROV_PLACEHOLDER__", _json.dumps(node_provenance, indent=2))
        node_path.write_text(html_content, encoding="utf-8")
        log.info("SelfDocumenter: wrote phase documentation node to %s", node_path)
        return str(node_path)

    # ------------------------------------------------------------------
    # Skill node — auto-extracted after successful multi-step goals
    # ------------------------------------------------------------------

    def document_completed_goal(
        self,
        goal_text: str,
        phases_completed: list[str],
        task_id: str,
        changed_files: list[str] | None = None,
        tests_status: str = "not run",
        executable_code: str | None = None,
    ) -> dict[str, str]:
        """
        Generate or **append to** a *skill* HTML Neural Node for a completed
        multi-step goal, then anchor the content hash on the local PoA blockchain.

        Append semantics (Q2)
        ---------------------
        The node id is deterministic (``skill-{goal_slug}``) so the same file
        is updated on every successful re-run.  Rather than overwriting the
        whole file, each run *appends* a timestamped ``<section>`` block to
        the ``<section id="history">`` element inside the article.  This
        preserves all prior runs and allows the operator to revert to an
        earlier recipe by inspecting the history section.

        The ``<section id="procedure">`` and ``<section id="semantic-memory">``
        sections are always updated to reflect the *latest* run, keeping them
        accurate for activation / context injection.

        Blockchain anchor (Q2 / Q3)
        ---------------------------
        After writing the file a SHA-256 hash of the full HTML is computed and
        POSTed to the local Tala PoA chain at ``http://127.0.0.1:8332/chain/tx``
        as a ``node-create`` transaction.  This mirrors the pattern used by
        ``identity.save_user_profile``.  If the chain node is offline the error
        is suppressed — the skill node is still written.

        Parameters
        ----------
        goal_text:
            The original natural-language goal string.
        phases_completed:
            Ordered list of phase description strings that succeeded.
        task_id:
            Parent task UUID from GoalEngine (for cross-referencing).
        changed_files:
            Optional list of file paths touched during execution.
        tests_status:
            Summary of test outcome, e.g. "All passed" or "not run".

        Returns
        -------
        dict[str, str]
            Paths to the written skill node and the learning event node.
        """
        import hashlib as _hashlib
        import json as _json
        import re as _re
        import urllib.request as _urllib

        from sdk.agent.documentation_nodes import assess_skill_quality

        # Not every completed goal is a skill. This path used to mint a node
        # per goal, slugged from the raw goal text, and seven of the eight that
        # accumulated were junk -- one-off test fixes, "Workflow: Unnamed
        # Workflow", two blank titles. Skill bodies are raw Python and the wiki
        # is a retrieval source, so that junk actively hijacked responses: asked
        # to run a self-test, Tala retrieved one and reproduced its file
        # contents instead of calling a tool.
        _skill_rejection = assess_skill_quality(
            goal_text, phases_completed, executable_code
        )
        if _skill_rejection:
            log.info(
                "SelfDocumenter: no skill node for %r -- %s",
                goal_text[:60], _skill_rejection,
            )
            return {
                "status": "skipped",
                "reason": _skill_rejection,
                "skill_node_path": "",
                "learning_node_path": "",
            }

        # ── Deterministic slug / node id ──────────────────────────────────
        slug = _re.sub(r"[^a-z0-9]+", "_", goal_text.lower().strip())[:60].strip("_")
        node_id = f"skill-{slug}"
        node_filename = f"{node_id}.html"

        nodes_dir = Path(self.root_dir) / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)
        node_path = nodes_dir / node_filename

        timestamp = datetime.now(UTC).isoformat()

        # ── Keyword extraction (unique words ≥ 5 chars) ───────────────────
        words = _re.findall(r"[a-zA-Z]{5,}", goal_text + " " + " ".join(phases_completed))
        keywords = ", ".join(sorted(set(w.lower() for w in words)))[:300]

        # ── Build this run's data block ───────────────────────────────────
        phases_inline = " | ".join(phases_completed)
        procedure_items = "".join(f"<li>{p}</li>" for p in phases_completed)
        files_html = (
            "".join(f"<li><code>{f}</code></li>" for f in (changed_files or []))
            or "<li><em>No file list recorded.</em></li>"
        )

        state_dict = {
            "goal": goal_text,
            "phases": phases_completed,
            "task_id": task_id,
            "phase_count": len(phases_completed),
            "changed_files": changed_files or [],
            "tests_status": tests_status,
            "recorded_at": timestamp,
            "metrics": {
                "version": 1,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": 0.0,
                "confidence": 0.8,
            }
        }

        # ── History block for this run (appended, never overwritten) ──────
        history_entry = f"""
  <article class="skill-run" data-task-id="{task_id}" data-recorded="{timestamp}">
    <h3>Run: <time datetime="{timestamp}">{timestamp[:19].replace("T", " ")} UTC</time></h3>
    <p><strong>Task ID:</strong> <code>{task_id}</code></p>
    <p><strong>Phases ({len(phases_completed)}):</strong> {phases_inline}</p>
    <p><strong>Tests:</strong> {tests_status}</p>
    <ol>
      {procedure_items}
    </ol>
    <p><strong>Artifacts:</strong></p>
    <ul>
      {files_html}
    </ul>
  </article>"""

        # ── Create or append ───────────────────────────────────────────────
        if node_path.exists():
            # ── APPEND PATH: node already exists ──────────────────────────
            html = node_path.read_text(encoding="utf-8", errors="replace")

            # 1. Update semantic-memory (latest run)
            new_sem = (
                f"\n    <p>Goal: {goal_text}</p>\n"
                f"    <p>Phases: {phases_inline}</p>\n"
                f"    <p>Keywords: {keywords}</p>\n"
                f"    <p>Tests: {tests_status}</p>\n  "
            )
            sem_open = '<section id="semantic-memory">'
            sem_close = "</section>"
            if sem_open in html:
                start = html.index(sem_open) + len(sem_open)
                end = html.index(sem_close, start)
                html = html[:start] + new_sem + html[end:]

            # 2. Update procedure (latest run)
            new_proc = f"\n    <ol>\n      {procedure_items}\n    </ol>\n  "
            proc_open = '<section id="procedure">'
            if proc_open in html:
                start = html.index(proc_open) + len(proc_open)
                end = html.index(sem_close, start)
                html = html[:start] + new_proc + html[end:]

            # 3. Update node-state JSON (latest run)
            state_open = '<script type="application/json" id="node-state">'
            state_close = "</script>"
            if state_open in html:
                start = html.index(state_open) + len(state_open)
                end = html.index(state_close, start)
                
                # Parse old state to preserve metrics
                try:
                    old_state_json = html[start:end].strip()
                    old_state = _json.loads(old_state_json)
                    old_metrics = old_state.get("metrics", {})
                    state_dict["metrics"]["version"] = old_metrics.get("version", 0) + 1
                    state_dict["metrics"]["success_count"] = old_metrics.get("success_count", 0)
                    state_dict["metrics"]["failure_count"] = old_metrics.get("failure_count", 0)
                    state_dict["metrics"]["success_rate"] = old_metrics.get("success_rate", 0.0)
                    state_dict["metrics"]["confidence"] = old_metrics.get("confidence", 0.8)
                except Exception:
                    pass
                
                state_json = _json.dumps(state_dict, indent=2)
                html = html[:start] + f"\n{state_json}\n  " + html[end:]
            else:
                state_json = _json.dumps(state_dict, indent=2)

            # 4. APPEND history entry to <section id="history">
            hist_open = '<section id="history">'
            hist_close = "</section>"
            if hist_open in html:
                insert_at = html.index(hist_close, html.index(hist_open))
                html = html[:insert_at] + history_entry + "\n  " + html[insert_at:]
            else:
                # history section missing — inject before </article>
                inject = (
                    f'\n  <section id="history">\n'
                    f'    <h2>Run History</h2>\n'
                    f'    {history_entry}\n'
                    f'  </section>\n'
                )
                html = html.replace("</article>", inject + "</article>", 1)

            # 5. Update node-executable Python (latest run) if provided
            if executable_code:
                clean_exec = _re.sub(r"(?i)<think(?:ing)?>.*?</think(?:ing)?>", "", executable_code, flags=_re.DOTALL)
                clean_exec = _re.sub(r"(?i)<think(?:ing)?>.*$", "", clean_exec, flags=_re.DOTALL)
                m_code = _re.search(r"```(?:python|py)?\s*\n?(.*?)\n?\s*```", clean_exec, _re.DOTALL | _re.IGNORECASE)
                if m_code:
                    clean_exec = m_code.group(1).strip()
                executable_code = clean_exec.replace("—", "--").replace("–", "-").replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'").strip()

            if executable_code:
                code_open = '<script type="application/x-python" id="node-executable">'
                code_close = "</script>"
                if code_open in html:
                    start = html.index(code_open) + len(code_open)
                    end = html.index(code_close, start)
                    html = html[:start] + f"\n{executable_code}\n  " + html[end:]
                else:
                    # Inject before </article>
                    inject_code = f'\n  <script type="application/x-python" id="node-executable">\n{executable_code}\n  </script>\n'
                    html = html.replace("</article>", inject_code + "</article>", 1)

        else:
            state_json = _json.dumps(state_dict, indent=2)
            # ── CREATE PATH: first run, build the full scaffold ────────────
            state_json = _json.dumps(state_dict, indent=2, ensure_ascii=False)
        
        adapter_packet = {
            "activation_keywords": [],
            "tool_affordances": [],
            "forbidden_actions": [],
            "repair_policies": {},
            "validators": {},
            "confidence": 1.0
        }
        
        content_html = f"""
    <p>Goal: {goal_text}</p>
    <p>Phases: {phases_inline}</p>
    <p>Keywords: {keywords}</p>
    <p>Tests: {tests_status}</p>
    
    <h2>Procedure (Latest Recipe)</h2>
    <ol>
      {procedure_items}
    </ol>
    
    <h2>Run History</h2>
    {history_entry}"""
        
        core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
            node_id, content_html, state_dict, adapter_packet, creator="tala-goal-engine"
        )
        
        node_provenance = {
            "core_hash": f"sha256:{core_hash}",
            "chaincode_id": chaincode_id,
            "anchoring_status": anchoring_status,
            "raw_response": raw_response,
            "key_fingerprint": public_key if public_key else "unanchored",
            "parent_chaincode_ids": [],
            "created_by": "tala-goal-engine",
            "created_at": timestamp,
            "wrapper_hash": "..."
        }
        prov_json_placeholder = _json.dumps(node_provenance, indent=2)

        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Skill Node: {goal_text[:80]}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="skill"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.20"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.80"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">

  <header id="node-identity">
    <h1 id="node-title">Skill: {goal_text[:120]}</h1>
  </header>

  <section id="semantic-memory">
{content_html}
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul></ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{_json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>

""" + (f"""  <script type="application/x-python" id="node-executable">
{executable_code}
  </script>
""" if executable_code else "") + """
</article>
</body>
</html>
"""
        wrapper_hash = _hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
        node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
        html = html_template.replace("__PROV_PLACEHOLDER__", _json.dumps(node_provenance, indent=2))

        node_path.write_text(html, encoding="utf-8")
        log.info(
            "SelfDocumenter: wrote skill node to %s (goal=%r, phases=%d)",
            node_path, goal_text[:60], len(phases_completed),
        )

        learning_node_path = ""
        try:
            learning_result = create_learning_documentation_node(
                topic=goal_text,
                summary=(
                    f"Tala distilled a reusable learning event from the completed goal "
                    f"'{goal_text}' with {len(phases_completed)} phase(s) and tests status "
                    f"'{tests_status}'."
                ),
                canonical_node_id=node_id,
                details=[
                    f"Goal: {goal_text}",
                    f"Task ID: {task_id}",
                    f"Phases ({len(phases_completed)}): {phases_inline}",
                    f"Tests: {tests_status}",
                    f"Changed files: {', '.join(changed_files or []) or 'none recorded'}",
                ],
                source_files=[str(node_path)] + [str(path) for path in (changed_files or [])],
                references=[node_id, "system-tala-html-nn-memory-runtime"],
                relationships=[
                    {
                        "target_node_id": node_id,
                        "label": "Skill Node",
                        "relationship_type": "supports",
                        "relationship_weight": 0.92,
                        "navigation_role": "skill-node",
                    }
                ],
            )
            learning_node_path = str(learning_result.get("path", ""))
            if learning_node_path:
                log.info(
                    "SelfDocumenter: wrote learning node to %s (goal=%r)",
                    learning_node_path, goal_text[:60],
                )
            elif learning_result.get("status") == "skipped":
                # Expected for most goals. Finishing a task is not the same as
                # learning something, and this path was the dominant source of
                # junk pages in the wiki -- a node per completed goal, titled
                # with the raw goal text. The skill node above still records
                # that the work happened; only the "lesson" is withheld.
                log.info(
                    "SelfDocumenter: no learning node for %r -- %s",
                    goal_text[:60], learning_result.get("reason"),
                )
        except Exception as exc:
            log.warning("SelfDocumenter: failed to write learning node for %s: %s", goal_text[:80], exc)

        return {
            "skill_node_path": str(node_path),
            "learning_node_path": learning_node_path,
        }

    @classmethod
    def record_skill_execution(cls, node_path: str | Path, success: bool, error_msg: str | None = None) -> bool:
        """
        Record the execution result (success/failure) of a skill to update its metrics.
        Returns True if successful, False if the node file could not be updated.
        """
        import json
        from datetime import datetime, UTC
        
        path = Path(node_path)
        if not path.exists():
            return False
            
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
            state_open = '<script type="application/json" id="node-state">'
            state_close = "</script>"
            if state_open not in html:
                return False
                
            start = html.index(state_open) + len(state_open)
            end = html.index(state_close, start)
            
            try:
                state_dict = json.loads(html[start:end].strip())
            except Exception:
                return False
                
            metrics = state_dict.get("metrics", {
                "version": 1,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": 0.0,
                "confidence": 0.8
            })
            
            if success:
                metrics["success_count"] += 1
            else:
                metrics["failure_count"] += 1
                
            total_runs = metrics["success_count"] + metrics["failure_count"]
            if total_runs > 0:
                metrics["success_rate"] = round(metrics["success_count"] / total_runs, 3)
            
            # Simple confidence calculation
            base_confidence = 0.8
            success_weight = 0.05
            failure_weight = 0.15
            
            conf = base_confidence + (metrics["success_count"] * success_weight) - (metrics["failure_count"] * failure_weight)
            conf = max(0.01, min(0.99, conf))
            metrics["confidence"] = round(conf, 3)
            
            state_dict["metrics"] = metrics
            new_state_json = json.dumps(state_dict, indent=2)
            html = html[:start] + f"\n{new_state_json}\n  " + html[end:]
            
            # Append execution entry to history
            timestamp = datetime.now(UTC).isoformat()
            status_str = "success" if success else "failed"
            error_html = f"<p><strong>Error:</strong> {error_msg}</p>" if not success and error_msg else ""
            
            exec_entry = f'''
  <article class="skill-exec" data-recorded="{timestamp}">
    <h3>Execution: <time datetime="{timestamp}">{timestamp[:19].replace("T", " ")} UTC</time></h3>
    <p><strong>Status:</strong> {status_str}</p>
    {error_html}
  </article>'''
            
            hist_open = '<section id="history">'
            hist_close = "</section>"
            if hist_open in html:
                insert_at = html.index(hist_close, html.index(hist_open))
                html = html[:insert_at] + exec_entry + "\n  " + html[insert_at:]
                
            path.write_text(html, encoding="utf-8")
            return True
        except Exception as e:
            log.warning("SelfDocumenter: failed to record skill execution for %s: %s", path.name, e)
            return False

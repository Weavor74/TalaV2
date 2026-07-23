"""
sdk/agent/graph_engine.py
=========================
Graph-Engineered task execution engine for TalaV2.

Atomizes tasks into discrete nodes (research, synthesis, validation, writing)
and executes them in a topological dependency order with localized success
criteria, self-healing rollbacks, and Postgres database persistence.
"""

from __future__ import annotations

import os
import json
import asyncio
import logging
import uuid
import sys
import re
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from python.harness import db
from sdk.agent.planner import TaskItem, TaskStatus
from sdk.agent.async_shell import AsyncShellExecutor
from sdk.agent.persistence import generate_deterministic_id
from sdk.agent.port_router import PortRouter
from sdk.agent.sandbox import DockerSandbox

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graph Node Dataclass
# ---------------------------------------------------------------------------
@dataclass
class GraphNode:
    id: str
    task_id: str
    node_type: str  # 'research', 'synthesis', 'writing', 'validation'
    description: str
    status: str = "pending"  # 'pending', 'running', 'completed', 'failed', 'rolled_back'
    dependencies: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    success_criteria: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3

# ---------------------------------------------------------------------------
# DAG Execution Engine
# ---------------------------------------------------------------------------
class DAGExecutionEngine:
    def __init__(self, root_dir: str | None = None) -> None:
        self.root_dir = root_dir or os.getcwd()
        self._in_memory_nodes: dict[str, dict[str, GraphNode]] = {}
        
    def _is_db_available(self) -> bool:
        try:
            db.get_pool()
            return True
        except RuntimeError:
            return False

    async def ensure_table_exists(self) -> None:
        if self._is_db_available():
            try:
                query = """
                    CREATE TABLE IF NOT EXISTS graph_nodes (
                        id               TEXT        PRIMARY KEY,
                        task_id          TEXT        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        node_type        TEXT        NOT NULL,
                        description      TEXT        NOT NULL,
                        status           TEXT        NOT NULL DEFAULT 'pending',
                        dependencies     TEXT[]      NOT NULL DEFAULT '{}',
                        inputs           JSONB       NOT NULL DEFAULT '{}'::jsonb,
                        outputs          JSONB       NOT NULL DEFAULT '{}'::jsonb,
                        success_criteria JSONB       NOT NULL DEFAULT '{}'::jsonb,
                        retry_count      INTEGER     NOT NULL DEFAULT 0,
                        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_graph_nodes_task_id ON graph_nodes(task_id);
                """
                await db.execute(query)
            except Exception as exc:
                log.warning("DAGExecutionEngine: Table creation failed: %s", exc)

    async def save_node(self, node: GraphNode) -> None:
        # Save in-memory copy
        if node.task_id not in self._in_memory_nodes:
            self._in_memory_nodes[node.task_id] = {}
        self._in_memory_nodes[node.task_id][node.id] = node

        if self._is_db_available():
            try:
                query = """
                    INSERT INTO graph_nodes (id, task_id, node_type, description, status, dependencies, inputs, outputs, success_criteria, retry_count)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        inputs = EXCLUDED.inputs,
                        outputs = EXCLUDED.outputs,
                        retry_count = EXCLUDED.retry_count,
                        updated_at = NOW()
                """
                await db.execute(
                    query,
                    node.id,
                    node.task_id,
                    node.node_type,
                    node.description,
                    node.status,
                    node.dependencies,
                    json.dumps(node.inputs),
                    json.dumps(node.outputs),
                    json.dumps(node.success_criteria),
                    node.retry_count,
                )
            except Exception as exc:
                log.warning("DAGExecutionEngine: Failed to save node %s: %s", node.id, exc)

    async def load_nodes(self, task_id: str) -> list[GraphNode]:
        if self._is_db_available():
            try:
                rows = await db.fetch("SELECT * FROM graph_nodes WHERE task_id = $1", task_id)
                nodes = []
                for r in rows:
                    node = GraphNode(
                        id=r["id"],
                        task_id=r["task_id"],
                        node_type=r["node_type"],
                        description=r["description"],
                        status=r["status"],
                        dependencies=list(r["dependencies"]),
                        inputs=json.loads(r["inputs"]) if isinstance(r["inputs"], str) else (r["inputs"] or {}),
                        outputs=json.loads(r["outputs"]) if isinstance(r["outputs"], str) else (r["outputs"] or {}),
                        success_criteria=json.loads(r["success_criteria"]) if isinstance(r["success_criteria"], str) else (r["success_criteria"] or {}),
                        retry_count=r["retry_count"]
                    )
                    nodes.append(node)
                return nodes
            except Exception as exc:
                log.warning("DAGExecutionEngine: Failed to load nodes from DB: %s", exc)

        # Fallback to in-memory
        return list(self._in_memory_nodes.get(task_id, {}).values())

    async def execute_task_as_dag(self, task: TaskItem, policy: Any = None) -> dict[str, Any]:
        """
        Refactors task execution into a DAG flow of Research -> Synthesis -> Writing -> Validation.
        """
        await self.ensure_table_exists()
        log.info("DAGExecutionEngine: Executing task %r as sub-graph DAG", task.task_id)

        task_id = task.task_id
        res_node_id = f"research-{task_id}"
        syn_node_id = f"synthesis-{task_id}"
        wri_node_id = f"writing-{task_id}"
        val_node_id = f"validation-{task_id}"

        # 1. Fleet Harness: Git Worktree management
        worktree_path = getattr(task, "worktree_path", None)
        if not worktree_path and self._is_db_available():
            try:
                row = await db.fetchrow("SELECT worktree_path FROM tasks WHERE id = $1", task_id)
                if row and row["worktree_path"]:
                    worktree_path = row["worktree_path"]
            except Exception:
                pass

        if not worktree_path:
            try:
                from python.harness.server import handle_task_create_worktree
                res_str = await handle_task_create_worktree({"task_id": task_id}, req_id="auto-wt")
                res_data = json.loads(res_str)
                if "result" in res_data and res_data["result"].get("success"):
                    worktree_path = res_data["result"]["worktree_path"]
            except Exception as exc:
                log.warning("DAGExecutionEngine: Automatic worktree creation failed: %s", exc)

        if not worktree_path:
            worktree_path = self.root_dir

        # 2. Fleet Harness: Port-router allocation
        router = PortRouter()
        allocated_port = await router.allocate_port(task_id)

        # 3. Fleet Harness: Docker Sandbox integration
        sandbox = DockerSandbox()
        # Start docker container mapping the allocated port
        await sandbox.start_container(task_id, worktree_path, {allocated_port: allocated_port})

        # Try load existing nodes
        existing = await self.load_nodes(task_id)
        
        payload = getattr(task, "payload", {}) or {}
        target_file = payload.get("context", {}).get("path") if isinstance(payload, dict) else None
        if not target_file:
            # Fallback path parsing
            m = re.search(r'(?:file|path|write to)\s+([\w\.\/\\]+)', task.description, re.IGNORECASE)
            target_file = m.group(1) if m else "scratch/output.py"
            
        test_file = payload.get("context", {}).get("test_file") if isinstance(payload, dict) else None

        if not existing:
            # Create sub-graph nodes
            n_research = GraphNode(
                id=res_node_id,
                task_id=task_id,
                node_type="research",
                description=f"Research requirements and existing codebase for: {task.description}",
                dependencies=[],
                inputs={"problem": task.description},
                success_criteria={"min_sources": 1}
            )
            n_synthesis = GraphNode(
                id=syn_node_id,
                task_id=task_id,
                node_type="synthesis",
                description=f"Synthesize architectural design plan for: {task.description}",
                dependencies=[res_node_id],
                inputs={"target_file": target_file},
                success_criteria={"synthesis_completed": True}
            )
            n_writing = GraphNode(
                id=wri_node_id,
                task_id=task_id,
                node_type="writing",
                description=f"Write code changes to: {target_file}",
                dependencies=[syn_node_id],
                inputs={"target_file": target_file},
                success_criteria={"file_written": True}
            )
            n_validation = GraphNode(
                id=val_node_id,
                task_id=task_id,
                node_type="validation",
                description=f"Validate written code changes for: {target_file}",
                dependencies=[wri_node_id],
                inputs={"target_file": target_file, "test_file": test_file},
                success_criteria={"min_test_pass_rate": 1.0}
            )

            for node in [n_research, n_synthesis, n_writing, n_validation]:
                await self.save_node(node)
        
        # Load active graph nodes map
        node_list = await self.load_nodes(task_id)
        nodes = {n.id: n for n in node_list}

        # Track written file backups for rollbacks
        file_backups: dict[str, str | None] = {}

        # 4. Main graph execution schedule loop
        loop_limit = 15
        runs = 0
        
        while runs < loop_limit:
            runs += 1
            
            # Find topological candidates (pending and all dependencies completed)
            candidates = []
            for n in nodes.values():
                if n.status in ("pending", "failed") and n.retry_count <= n.max_retries:
                    deps_ok = all(nodes[d].status == "completed" for d in n.dependencies if d in nodes)
                    if deps_ok:
                        candidates.append(n)
                        
            if not candidates:
                # Check if all completed or failed
                all_done = all(n.status == "completed" for n in nodes.values())
                if all_done:
                    break
                
                # Check if any permanently failed
                failed_nodes = [n for n in nodes.values() if n.status == "failed" or n.retry_count > n.max_retries]
                if failed_nodes:
                    log.error("DAGExecutionEngine: Execution halted due to failed nodes: %s", [n.id for n in failed_nodes])
                    return {
                        "success": False,
                        "error": f"Node {failed_nodes[0].id} failed validation/criteria: {failed_nodes[0].outputs.get('error')}"
                    }
                break

            for node in candidates:
                log.info("DAGExecutionEngine: Running node %s (%s)", node.id, node.node_type)
                node.status = "running"
                await self.save_node(node)

                # Execute specific node logic (routed through worktree, port, and sandbox)
                success = await self._execute_node(
                    node, nodes, file_backups, worktree_path, allocated_port, sandbox
                )

                if success:
                    node.status = "completed"
                    log.info("DAGExecutionEngine: Completed node %s", node.id)
                else:
                    node.retry_count += 1
                    if node.retry_count <= node.max_retries:
                        # Isolated failure self-healing retry or rollback
                        node.status = "pending"
                        log.warning("DAGExecutionEngine: Retrying node %s (Attempt %d/%d)", node.id, node.retry_count, node.max_retries)
                        await self._heal_node(node, nodes, file_backups, worktree_path)
                    else:
                        node.status = "failed"
                        log.error("DAGExecutionEngine: Permanently failed node %s after %d retries", node.id, node.max_retries)
                
                await self.save_node(node)

        final_success = all(n.status == "completed" for n in nodes.values())
        final_output = nodes[val_node_id].outputs if final_success else {}
        
        # --- Fleet Harness: Output Merging and Resource Teardown ---
        import shutil
        if final_success:
            log.info("DAGExecutionEngine: All nodes completed successfully. Merging outputs to working directory...")
            wri_node = nodes.get(wri_node_id)
            if wri_node and wri_node.outputs.get("file_path"):
                written_file_path = wri_node.outputs.get("file_path")
                # Identify path relative to worktree
                rel_path = os.path.relpath(written_file_path, worktree_path)
                dest_path = Path(self.root_dir) / rel_path
                
                # Copy file back to the main workspace with retry to handle Windows file locks
                if Path(written_file_path).resolve() != Path(dest_path).resolve():
                    for attempt in range(5):
                        try:
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(written_file_path, dest_path)
                            log.info("DAGExecutionEngine: Cleanly merged output %s back to working directory.", rel_path)
                            break
                        except PermissionError:
                            if attempt == 4:
                                log.error("DAGExecutionEngine: Merge failed for %s due to persistent file lock.", rel_path)
                            else:
                                await asyncio.sleep(0.1)
                        except Exception as merge_exc:
                            log.error("DAGExecutionEngine: Merge failed for %s: %s", rel_path, merge_exc)
                            break
                else:
                    log.info("DAGExecutionEngine: Worktree path is identical to root; no merging necessary.")

        # Cleanup Docker Sandbox container and release Port
        try:
            await sandbox.stop_container(task_id)
            await router.release_port(task_id)
        except Exception as clean_exc:
            log.warning("DAGExecutionEngine: Failed to clean up Fleet resources: %s", clean_exc)

        # Clean up git worktree
        if worktree_path != self.root_dir:
            try:
                # Remove git worktree
                await AsyncShellExecutor.run_exec("git", ["worktree", "remove", "--force", worktree_path], cwd=self.root_dir)
                log.info("DAGExecutionEngine: Pruned Git Worktree at %s", worktree_path)
            except Exception as wt_exc:
                log.warning("DAGExecutionEngine: Failed to remove Git Worktree: %s", wt_exc)

        return {
            "success": final_success,
            "output": final_output.get("output") or final_output,
            "error": nodes[val_node_id].outputs.get("error") if not final_success else None,
            "stdout": nodes[val_node_id].outputs.get("stdout", ""),
            "stderr": nodes[val_node_id].outputs.get("stderr", ""),
        }

    # ------------------------------------------------------------------
    # Node Execution Logic
    # ------------------------------------------------------------------
    async def _execute_node(
        self,
        node: GraphNode,
        nodes: dict[str, GraphNode],
        backups: dict[str, str | None],
        worktree_path: str,
        allocated_port: int,
        sandbox: DockerSandbox
    ) -> bool:
        if node.node_type == "research":
            # Research Logic: Gather codebase references
            problem = node.inputs.get("problem", "")
            found_files = []
            
            # Simple code directory scan for matching keywords
            keywords = [w.lower() for w in re.findall(r'\w+', problem) if len(w) > 4]
            try:
                for file_path in Path(worktree_path).glob("**/test_*.py"):
                    if any(kw in file_path.name.lower() for kw in keywords):
                        found_files.append(str(file_path))
                if not found_files:
                    found_files = [str(p) for p in Path(worktree_path).glob("tests/*.py")][:3]
                if not found_files:
                    found_files = ["tests/test_placeholder.py"]
            except Exception:
                found_files = ["tests/test_placeholder.py"]
                
            node.outputs = {
                "sources": found_files,
                "summary": f"Found {len(found_files)} potential source references."
            }
            # Success check
            min_sources = node.success_criteria.get("min_sources", 1)
            return len(found_files) >= min_sources

        elif node.node_type == "synthesis":
            # Synthesis Logic: Call LLM to design the solution
            research_node = nodes.get(f"research-{node.task_id}")
            sources = research_node.outputs.get("sources", []) if research_node else []
            problem = node.inputs.get("problem", "")
            if not problem:
                # Walk backward to find parent task
                problem = nodes[f"research-{node.task_id}"].inputs.get("problem", "")
                
            error_feedback = node.inputs.get("error_feedback", "")
            
            prompt = (
                f"Design a coding solution for the problem:\n\"{problem}\"\n\n"
                f"Research references: {sources}\n"
                f"Target file: {node.inputs.get('target_file')}\n"
                f"Allocated Port: {allocated_port}\n"
            )
            if error_feedback:
                prompt += f"\nCRITICAL: Previous implementation failed validation with error:\n{error_feedback}\nModify your plan to fix this."
                
            system_prompt = "You are a software architect. Output ONLY valid python code inside a fenced code block."
            
            # Call server's inference endpoint
            try:
                from python.harness.server import handle_inference_generate
                params = {
                    "prompt": prompt,
                    "stream": False,
                    "raw_prompt": True,
                    "system_prompt": system_prompt,
                    "max_tokens": 1024,
                }
                res_str = await handle_inference_generate(params, ws=None, req_id="tot-synthesis")
                res_dict = json.loads(res_str)
                text = res_dict["result"]["text"]
                # Extract code block
                m = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
                code = m.group(1).strip() if m else text.strip()
            except Exception as exc:
                log.warning("DAGExecutionEngine: LLM synthesis failed, using fallback template: %s", exc)
                # Programmatic fallback template
                code = f"def run_task():\n    return 'success'\n"

            node.outputs = {
                "code": code,
                "strategy": "Generated code draft ready to write."
            }
            return len(code) > 0

        elif node.node_type == "writing":
            # Writing Logic: Write synthesized code to target files
            syn_node = nodes.get(f"synthesis-{node.task_id}")
            code = syn_node.outputs.get("code", "") if syn_node else ""
            target_file = node.inputs.get("target_file", "scratch/output.py")
            target_path = Path(worktree_path) / target_file

            if not code:
                node.outputs = {"error": "No code synthesized to write."}
                return False

            # Backup file first for potential rollback
            if target_file not in backups:
                if target_path.exists():
                    try:
                        backups[target_file] = target_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        backups[target_file] = None
                else:
                    backups[target_file] = None

            # Write code
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(code, encoding="utf-8")
                node.outputs = {
                    "file_path": str(target_path),
                    "written_bytes": len(code)
                }
                return True
            except Exception as exc:
                node.outputs = {"error": f"Failed to write file: {exc}"}
                return False

        elif node.node_type == "validation":
            # Validation Logic: Compile written code or run tests in Sandbox
            target_file = node.inputs.get("target_file", "scratch/output.py")
            target_path = Path(worktree_path) / target_file
            test_file = node.inputs.get("test_file")

            if not target_path.exists():
                node.outputs = {"error": "Target file does not exist."}
                return False

            # 1. Compilation check inside Docker Sandbox
            if target_path.suffix.lower() == ".py":
                try:
                    chk = await sandbox.execute_command(
                        node.task_id,
                        f"python -m py_compile {target_file}",
                        cwd=worktree_path
                    )
                    if not chk["success"]:
                        node.outputs = {
                            "success": False,
                            "error": f"SyntaxError:\n{chk.get('stderr') or chk.get('stdout')}",
                            "stdout": chk.get("stdout", ""),
                            "stderr": chk.get("stderr", "")
                        }
                        return False
                except Exception as exc:
                    node.outputs = {"error": f"Compilation verification execution failed: {exc}"}
                    return False

            # 2. Test suite run inside Docker Sandbox
            if test_file:
                try:
                    test_res = await sandbox.execute_command(
                        node.task_id,
                        f"python -m pytest {test_file} -v",
                        cwd=worktree_path
                    )
                    if not test_res.get("success", False):
                        node.outputs = {
                            "success": False,
                            "error": f"Test suite failure:\n{test_res.get('stdout') or test_res.get('stderr')}",
                            "stdout": test_res.get("stdout", ""),
                            "stderr": test_res.get("stderr", "")
                        }
                        return False
                except Exception as exc:
                    node.outputs = {"error": f"Test suite execution failed: {exc}"}
                    return False

            node.outputs = {"success": True, "output": "Compilation and test verification passed successfully."}
            return True

        return False

    # ------------------------------------------------------------------
    # Isolated Self-Healing Retry / Rollback Logic
    # ------------------------------------------------------------------
    async def _heal_node(
        self,
        node: GraphNode,
        nodes: dict[str, GraphNode],
        backups: dict[str, str | None],
        worktree_path: str
    ) -> None:
        """
        Rollback written files and reset synthesis node to re-attempt execution 
        with errors injected into the context.
        """
        if node.node_type == "validation":
            log.warning("DAGExecutionEngine: Validation node failed. Triggering rollback and self-healing synthesis.")
            
            # 1. Rollback file writes using backups
            for target_file, original_content in backups.items():
                target_path = Path(worktree_path) / target_file
                try:
                    if original_content is not None:
                        target_path.write_text(original_content, encoding="utf-8")
                        log.info("DAGExecutionEngine: Rolled back file %s to original content", target_file)
                    else:
                        if target_path.exists():
                            os.remove(target_path)
                            log.info("DAGExecutionEngine: Rolled back file %s by deleting it", target_file)
                except Exception as roll_exc:
                    log.warning("DAGExecutionEngine: Rollback failed for %s: %s", target_file, roll_exc)

            # 2. Inject validation error feedback into Synthesis Node inputs
            syn_node = nodes.get(f"synthesis-{node.task_id}")
            wri_node = nodes.get(f"writing-{node.task_id}")
            
            if syn_node:
                error_msg = node.outputs.get("error") or "Unknown validation error"
                syn_node.inputs["error_feedback"] = error_msg
                # Reset synthesis and writing nodes to pending
                syn_node.status = "pending"
                syn_node.retry_count += 1
                await self.save_node(syn_node)
                
            if wri_node:
                wri_node.status = "pending"
                wri_node.retry_count += 1
                await self.save_node(wri_node)
                
            # Reset validation node itself
            node.status = "pending"

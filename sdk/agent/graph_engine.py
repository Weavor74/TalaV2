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


#: Ceiling for a single synthesis LLM call, in seconds. Override with
#: TALA_SYNTHESIS_TIMEOUT for slower or faster hardware.
#:
#: This was 60s, which no local model on this hardware can meet -- measured
#: chat turns against the local 8B run 60-160s. Synthesis therefore failed on
#: the clock every time, and the fallback that reused a stale file reported
#: that failure as a completed phase.
SYNTHESIS_TIMEOUT_SECONDS = float(os.getenv("TALA_SYNTHESIS_TIMEOUT", "300"))

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

    # ------------------------------------------------------------------
    # Workflow spec: the executable HTML-NN chain definition
    # ------------------------------------------------------------------
    #: Node types _execute_node knows how to run. A spec step naming
    #: anything else is a spec error, not a new capability.
    SUPPORTED_STEP_TYPES = frozenset({"research", "synthesis", "writing", "validation", "tool"})
    WORKFLOW_NODE_FILENAME = "workflow-self-extension.html"

    async def _resolve_persona_prompt(self, agent_id: str | None) -> str:
        """
        Look up a persona's system prompt by agent id.

        Returns "" for no persona, which is the default and the normal case.
        A persona that was deleted after the topology was authored is logged
        as a warning rather than failing the node: losing the character is
        worth continuing through, but it must not be silent, because the run
        would otherwise look identical to one that never named a persona.
        """
        if not agent_id:
            return ""
        try:
            row = await db.fetchrow("SELECT name, config FROM agents WHERE id = $1", agent_id)
        except Exception:
            log.warning("Could not resolve persona %r (no database) — running node without it", agent_id)
            return ""
        if not row:
            log.warning(
                "Node names persona %r, which no longer exists — running node without it", agent_id
            )
            return ""

        config = row["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except Exception:
                config = {}
        prompt = (config or {}).get("system_prompt", "").strip()
        if not prompt:
            log.info("Persona %r has no system prompt; node runs without one", agent_id)
            return ""
        log.info("Node running as persona %r (%s)", agent_id, row["name"])
        return prompt

    def _load_workflow_spec(self) -> dict[str, Any] | None:
        """
        Load the self-extension chain definition from the workflow HTML-NN,
        if one exists. Returns None (built-in chain) when the node is absent
        or its spec is invalid — the loop must keep closing even if the node
        she edited no longer parses, so every rejection is logged with the
        reason rather than raised.

        The node is the single canonical source (resolve_active_nodes_dir);
        there is deliberately no second lookup location.
        """
        try:
            from sdk.agent.documentation_nodes import resolve_active_nodes_dir
            from sdk.agent.parser import parse_file

            node_path = resolve_active_nodes_dir() / self.WORKFLOW_NODE_FILENAME
            if not node_path.exists():
                return None

            parsed = parse_file(str(node_path))
            spec = (parsed.state_data or {}).get("workflow")
            if not isinstance(spec, dict):
                log.warning("Workflow node %s has no 'workflow' object in #node-state — using built-in chain", node_path)
                return None

            steps = spec.get("steps")
            if not isinstance(steps, list) or not steps:
                log.warning("Workflow node spec has no steps — using built-in chain")
                return None

            seen: set[str] = set()
            for step in steps:
                if not isinstance(step, dict):
                    log.warning("Workflow node spec: step is not an object — using built-in chain")
                    return None
                step_id = str(step.get("step") or "").strip()
                node_type = str(step.get("node_type") or "").strip()
                if not re.fullmatch(r"[a-z][a-z0-9_-]*", step_id):
                    log.warning("Workflow node spec: bad step id %r — using built-in chain", step_id)
                    return None
                if step_id in seen:
                    log.warning("Workflow node spec: duplicate step id %r — using built-in chain", step_id)
                    return None
                if node_type not in self.SUPPORTED_STEP_TYPES:
                    log.warning(
                        "Workflow node spec: step %r has unsupported node_type %r (supported: %s) — using built-in chain",
                        step_id, node_type, sorted(self.SUPPORTED_STEP_TYPES),
                    )
                    return None
                deps = step.get("depends_on") or []
                if not isinstance(deps, list) or any(d not in seen for d in deps):
                    # Steps must be listed in dependency order; a forward or
                    # unknown reference is either a typo or a cycle.
                    log.warning(
                        "Workflow node spec: step %r depends on undefined/later step(s) %s — using built-in chain",
                        step_id, [d for d in deps if d not in seen],
                    )
                    return None
                seen.add(step_id)

            return spec
        except Exception as exc:
            log.warning("Workflow node could not be loaded (%s) — using built-in chain", exc)
            return None

    @staticmethod
    def _first_node_of_type(nodes: dict[str, GraphNode], node_type: str) -> GraphNode | None:
        """First graph node of the given type, in insertion (spec/chain) order."""
        for n in nodes.values():
            if n.node_type == node_type:
                return n
        return None

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
        Public entry point. Registers task_id as "actively executing" for the
        duration of the run and *always* unregisters it on the way out —
        success, failure, or exception — via try/finally around the real
        work in _execute_task_as_dag_inner.

        Regression: this used to only ever call register_active_task, never
        unregister_active_task, on any exit path. sdk.agent.goal_engine's
        _ACTIVE_TASK_IDS grew and never shrank, and ZombieReaper.reclaim_zombies
        (sdk/agent/swarm_engine.py) treats any task_id in that set as "still
        actively executing" and skips reclaiming it — so once a task_id had
        ever run through this engine once, a later stuck/crashed run of that
        same id could never be reclaimed as a zombie.
        """
        task_id = task.task_id
        try:
            from sdk.agent.goal_engine import register_active_task
            register_active_task(task_id)
        except Exception:
            pass

        try:
            return await self._execute_task_as_dag_inner(task, policy)
        finally:
            try:
                from sdk.agent.goal_engine import unregister_active_task
                unregister_active_task(task_id)
            except Exception:
                pass


    #: Files a plan step is asking to be written.
    DEFAULT_TARGET = "scratch/output.py"

    @classmethod
    def derive_target_files(
        cls,
        description: str,
        full_goal: str = "",
        explicit_path: str | None = None,
    ) -> list[str]:
        """
        Every file this step should write, most-certain first.

        Extracted so the regexes are testable without building and running a
        graph -- they carry most of the risk in this module and had produced
        two separate real incidents before this one.

        A goal that says "run X" or "execute X" is NOT asking for X to be
        rewritten. The scrape is greedy enough to pull any path out of prose,
        and every graph ends in a `writing` node, so "run
        scripts/generate_stories.py" once produced a plan to *overwrite*
        scripts/generate_stories.py with freshly synthesised code. That
        happened twice for real, replacing a 400-line generator with a
        hello-world, and validation then compiled the placeholder and reported
        success.

        The step's own description is the only source of *additional* targets.
        Taking them from the whole goal would make an early phase write files a
        later phase owns.
        """
        if explicit_path:
            return [str(explicit_path)]

        execute_only = re.search(
            r'\b(run|execute|invoke|call)\b[^.\n]{0,40}?[\w\-]+[\/\\][\w\-\/\.]+\.[a-zA-Z0-9]+',
            f"{description}\n{full_goal}",
            re.IGNORECASE,
        )

        primary = None
        if not execute_only:
            for text in (description, full_goal):
                # "named"/"called" cover creation goals naming a bare filename
                # with no directory ("Create a script named hello.py") -- those
                # used to fall through to the default, so the task went green
                # while the requested file never existed.
                # Write-intent words only. "at" and "in" are positional and
                # describe where to *read* at least as often as where to
                # write: "Write report.py using the data in scratch/tmp.py"
                # resolved to scratch/tmp.py, which would have overwritten
                # the data source. They are tried last instead, below.
                m = re.search(
                    r'(?:file|path|write to|into|named|called)\s+([\w\.\/\\]+\.[a-zA-Z0-9]+)',
                    text, re.IGNORECASE,
                )
                if m:
                    primary = m.group(1)
                    break
                # A bare filename with no directory and no introducing word.
                #
                # "Then refactor both user.py and order.py to import the
                # Logger" matched neither pattern above -- the first needs an
                # introducing word, the second needs a slash -- so the step
                # fell through to scratch/output.py and wrote there. The files
                # it named were never touched, which is the whole of why the
                # multi-file benchmark failed on every engine.
                #
                # Restricted to extensions a plan plausibly writes, so prose
                # mentioning "version 2.5" or "the 3.11 runtime" cannot become
                # a write target.
                m3 = re.search(
                    r'(?<![\w\/\\.])([\w\-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|html|css|sql|sh|ps1))\b',
                    text, re.IGNORECASE,
                )
                if m3:
                    primary = m3.group(1)
                    break
                m2 = re.search(r'([\w\-]+[\/\\][\w\-\/\.]+\.[a-zA-Z0-9]+)', text)
                if m2:
                    primary = m2.group(1)
                    break
                # Last resort: a positional preposition. Reached only when
                # the step named nothing more definite, so "in X" is the
                # best available guess rather than a preferred reading.
                m4 = re.search(
                    r'(?:at|in)\s+([\w\.\/\\]+\.[a-zA-Z0-9]+)', text, re.IGNORECASE,
                )
                if m4:
                    primary = m4.group(1)
                    break

        if not primary:
            return [cls.DEFAULT_TARGET]

        targets = [primary]
        if not execute_only:
            for extra in re.findall(
                r'\b([\w\-]+(?:[\/\\][\w\-\.]+)*\.[a-zA-Z0-9]+)\b', str(description)
            ):
                if extra not in targets and not extra.startswith("scratch/"):
                    targets.append(extra)
        return targets

    async def _execute_task_as_dag_inner(self, task: TaskItem, policy: Any = None) -> dict[str, Any]:
        """
        Refactors task execution into a DAG flow of Research -> Synthesis -> Writing -> Validation.
        """
        try:
            await asyncio.wait_for(self.ensure_table_exists(), timeout=10.0)
        except Exception as exc:
            log.warning("DAGExecutionEngine: ensure_table_exists failed/timed out: %s — continuing.", exc)
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
                row = await asyncio.wait_for(
                    db.fetchrow("SELECT worktree_path FROM tasks WHERE id = $1", task_id),
                    timeout=5.0
                )
                if row and row["worktree_path"]:
                    worktree_path = row["worktree_path"]
            except Exception:
                pass

        # Git-worktree isolation exists to protect the install checkout from
        # concurrent fleet tasks. When this engine was constructed with a
        # CUSTOM root_dir (benchmarks, a scoped workspace), that directory is
        # already the isolation boundary — auto-creating a worktree of the
        # REPO here meant every phase ran in a fresh repo checkout unrelated
        # to root_dir: phase 1's outputs were written into worktree A (then
        # destroyed with it at teardown), and phase 2's worktree B never saw
        # them, so any multi-phase goal against a custom workspace was
        # incoherent by construction.
        _is_repo_root = True
        try:
            from sdk.agent.documentation_nodes import _application_root
            _is_repo_root = Path(self.root_dir).resolve() == _application_root().resolve()
        except Exception:
            pass

        if not worktree_path and not _is_repo_root:
            worktree_path = self.root_dir

        if not worktree_path:
            try:
                from python.harness.server import handle_task_create_worktree
                res_str = await asyncio.wait_for(
                    handle_task_create_worktree({"task_id": task_id}, req_id="auto-wt"),
                    timeout=3.0
                )
                res_data = json.loads(res_str)
                if "result" in res_data and res_data["result"].get("success"):
                    worktree_path = res_data["result"]["worktree_path"]
            except Exception as exc:
                log.warning("DAGExecutionEngine: Automatic worktree creation bypassed/timed out: %s — using root workspace", exc)

        if not worktree_path:
            worktree_path = self.root_dir

        # 2. Fleet Harness: Port-router allocation
        router = PortRouter()
        allocated_port = await router.allocate_port(task_id)

        # No sandbox/container — all execution is direct on the host.

        # Try load existing nodes
        existing = await self.load_nodes(task_id)
        
        payload = getattr(task, "payload", {}) or {}
        
        # Resolve full goal text for LLM synthesis context
        full_goal = task.description
        if isinstance(payload, dict):
            full_goal = payload.get("goal") or payload.get("user_goal") or payload.get("description") or task.description

            # Fold in a curated summary of earlier phases in the same multi-phase
            # goal, if the caller (GoalEngine.run_goal) supplied one. Without
            # this, each phase only ever saw the original goal text plus its
            # own description -- a "coder" phase had no visibility into what a
            # preceding "planner" phase actually decided, so downstream phases
            # re-derived from scratch instead of building on prior work. The
            # summary is pre-truncated by the caller specifically so this
            # doesn't reintroduce the unbounded-context-growth problem it's
            # meant to solve.
            prior_summary = (payload.get("context") or {}).get("prior_phase_summary")
            if prior_summary:
                full_goal = f"{full_goal}\n\n{prior_summary}"

        target_files = self.derive_target_files(
            task.description,
            full_goal,
            (payload.get("context", {}) or {}).get("path") if isinstance(payload, dict) else None,
        )
        target_file = target_files[0]

        test_file = payload.get("context", {}).get("test_file") if isinstance(payload, dict) else None

        # A plan step that names a tool must CALL that tool.
        #
        # The decomposer already emits {"tool": "schedule_goal", "tool_params":
        # {...}} correctly, but until now nothing consumed it: the only node
        # types were research/synthesis/writing/validation, so every
        # tool-shaped step fell through the code-writing pipeline instead. The
        # task then completed green off the back of a compile check, having
        # never invoked the tool. That made *every* tool-requiring autonomous
        # goal silently a no-op -- scheduling, messaging, lookups, all of it.
        tool_name = payload.get("tool") if isinstance(payload, dict) else None

        # The chain definition is loaded from the workflow HTML-NN when one
        # exists, so the self-extension process is itself a node she can read
        # and edit. The built-in chain below is the fallback, not the source
        # of truth.
        workflow_spec = self._load_workflow_spec()
        self._active_workflow_spec = workflow_spec

        # A swarm node can name the persona it runs as (TopologyNode.agent_id).
        # Resolved once per task and held on the instance, since one engine
        # instance executes exactly one task.
        self._persona_prompt = await self._resolve_persona_prompt(
            payload.get("agent_id") if isinstance(payload, dict) else None
        )

        if not existing:
            chain: list[GraphNode] = []
            if tool_name:
                # A tool-shaped plan step becomes a one-node chain that runs
                # through the normal execution loop below. (This used to
                # build the node and then return it without ever executing
                # it — the early return also called a save_nodes() method
                # that never existed, so the first tool-shaped plan step to
                # reach the DAG in production crashed the whole task.)
                chain = [GraphNode(
                    id=f"tool-{task_id}",
                    task_id=task_id,
                    node_type="tool",
                    description=f"Invoke tool {tool_name}: {task.description}",
                    dependencies=[],
                    inputs={
                        "tool": tool_name,
                        "tool_params": payload.get("tool_params") or {},
                        # Carried from the compiled AdapterPacket via GoalEngine.
                        "governance": {
                            k: v
                            for k, v in (payload.get("context") or {}).items()
                            if k in ("forbidden_actions", "tool_affordances")
                        },
                    },
                    success_criteria={"tool_succeeded": True},
                )]
            elif workflow_spec:
                for step in workflow_spec["steps"]:
                    step_id = step["step"]
                    desc = str(step.get("description") or f"{step['node_type']} step for: {task.description}")
                    desc = desc.replace("{goal}", task.description).replace("{target_file}", target_file)
                    inputs: dict[str, Any] = {
                        "problem": full_goal,
                        "target_file": target_file,
                        "test_file": test_file,
                    }
                    config = step.get("config")
                    if isinstance(config, dict):
                        inputs.update(config)
                    chain.append(GraphNode(
                        id=f"{step_id}-{task_id}",
                        task_id=task_id,
                        node_type=step["node_type"],
                        description=desc,
                        dependencies=[f"{d}-{task_id}" for d in (step.get("depends_on") or [])],
                        inputs=inputs,
                        success_criteria=dict(step.get("success_criteria") or {}),
                        max_retries=int(step.get("max_retries", 3)),
                    ))
                log.info(
                    "DAGExecutionEngine: Built chain from workflow node (%d steps: %s)",
                    len(chain), [s["step"] for s in workflow_spec["steps"]],
                )
            else:
                chain = [
                    GraphNode(
                        id=res_node_id,
                        task_id=task_id,
                        node_type="research",
                        description=f"Research requirements and existing codebase for: {task.description}",
                        dependencies=[],
                        inputs={"problem": full_goal},
                        success_criteria={"min_sources": 1}
                    ),
                ]

                # One synthesise / write / validate chain per target file.
                #
                # They share the research node and run in sequence, so a later
                # file is written knowing the earlier one landed -- which is
                # what "refactor both user.py and order.py to import the Logger
                # from common.py" needs, and what a single chain could never
                # express. The single-target case builds exactly the graph it
                # always did, with the same node ids.
                previous = res_node_id
                for index, path in enumerate(target_files):
                    suffix = "" if index == 0 else f"-{index}"
                    syn_id = f"{syn_node_id}{suffix}"
                    wri_id = f"{wri_node_id}{suffix}"
                    val_id = f"{val_node_id}{suffix}"
                    chain.extend([
                        GraphNode(
                            id=syn_id,
                            task_id=task_id,
                            node_type="synthesis",
                            description=f"Synthesize architectural design plan for: {task.description}",
                            dependencies=[previous],
                            inputs={"problem": full_goal, "target_file": path},
                            success_criteria={"synthesis_completed": True}
                        ),
                        GraphNode(
                            id=wri_id,
                            task_id=task_id,
                            node_type="writing",
                            description=f"Write code changes to: {path}",
                            dependencies=[syn_id],
                            inputs={"target_file": path},
                            success_criteria={"file_written": True}
                        ),
                        GraphNode(
                            id=val_id,
                            task_id=task_id,
                            node_type="validation",
                            description=f"Validate written code changes for: {path}",
                            dependencies=[wri_id],
                            inputs={"target_file": path, "test_file": test_file},
                            success_criteria={"min_test_pass_rate": 1.0}
                        ),
                    ])
                    previous = val_id

            is_verification_phase = any(kw in task.description.lower() for kw in ("verify and compile", "verify solution", "validate solution"))
            if is_verification_phase:
                log.info("DAGExecutionEngine: Task '%s' is a verification phase — short-circuiting synthesis & writing nodes.", task.description)
                chain_by_id = {n.id: n for n in chain}
                res_ids = [n.id for n in chain if n.node_type == "research"]
                for n in chain:
                    if n.node_type == "synthesis":
                        n.status = "completed"
                        n.outputs = {"code": "# Verification phase — pre-completed"}
                    elif n.node_type == "writing":
                        n.status = "completed"
                        n.outputs = {"file_path": str(Path(worktree_path) / target_file)}
                    elif n.node_type == "validation":
                        # Depend only on whatever research steps exist, since
                        # the synthesis/writing steps are pre-completed.
                        n.dependencies = [d for d in n.dependencies if chain_by_id.get(d) and chain_by_id[d].node_type not in ("synthesis", "writing")] or res_ids

            for node in chain:
                await self.save_node(node)
        
        # Load active graph nodes map
        node_list = await self.load_nodes(task_id)
        nodes = {n.id: n for n in node_list}

        # Reset any stuck 'running' or 'failed' state on task startup so re-execution works
        for n in nodes.values():
            if n.status == "running":
                log.info("DAGExecutionEngine: Resetting stuck node %s from 'running' to 'pending'", n.id)
                n.status = "pending"
            if n.status == "failed":
                log.info("DAGExecutionEngine: Resetting failed node %s to 'pending' (clearing retry count)", n.id)
                n.status = "pending"
                n.retry_count = 0
            if target_file != "scratch/output.py":
                n.inputs["target_file"] = target_file
            if full_goal:
                n.inputs["problem"] = full_goal
            await self.save_node(n)

        log.info(
            "DAGExecutionEngine: Initialized graph nodes for task %s: %s",
            task_id,
            {nid: f"status={n.status}, retries={n.retry_count}" for nid, n in nodes.items()}
        )

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

            log.info(
                "DAGExecutionEngine: Loop #%d — Candidates: %s | Node states: %s",
                runs,
                [c.id for c in candidates],
                {nid: n.status for nid, n in nodes.items()}
            )

            # Heartbeat: prevent ZombieReaper from killing long-running tasks (e.g. LLM synthesis)
            try:
                from sdk.agent.telemetry import LiveStatusReporter
                await LiveStatusReporter.broadcast_status(
                    task_id=task_id,
                    phase="DAGExecution",
                    status="in_progress",
                    message=f"DAG loop #{runs}: {len(candidates)} candidate(s) ready",
                    details={"loop": runs, "candidates": [c.id for c in candidates]}
                )
            except Exception:
                pass
                        
            if not candidates:
                # Check if all completed or failed
                all_done = all(n.status == "completed" for n in nodes.values())
                if all_done:
                    log.info("DAGExecutionEngine: All DAG nodes successfully completed.")
                    break
                
                # Check if any permanently failed
                failed_nodes = [n for n in nodes.values() if n.status == "failed" or n.retry_count > n.max_retries]
                if failed_nodes:
                    log.error("DAGExecutionEngine: Execution halted due to failed nodes: %s", [n.id for n in failed_nodes])
                    return {
                        "success": False,
                        "error": f"Node {failed_nodes[0].id} failed validation/criteria: {failed_nodes[0].outputs.get('error')}"
                    }
                log.warning("DAGExecutionEngine: No candidates available and graph incomplete. Halted loop.")
                break

            for node in candidates:
                log.info("DAGExecutionEngine: Running node %s (%s)", node.id, node.node_type)
                node.status = "running"
                await self.save_node(node)

                # Execute specific node logic
                success = await self._execute_node(
                    node, nodes, file_backups, worktree_path, allocated_port
                )

                if success:
                    node.status = "completed"
                    log.info("DAGExecutionEngine: Successfully completed node %s (%s)", node.id, node.node_type)
                else:
                    # Some failures are not worth repeating. A node that failed
                    # because the model did not answer within its timeout will
                    # almost certainly not answer within the same timeout three
                    # more times -- it just costs another N x timeout of dead
                    # waiting. Measured on this box: a 60s synthesis timeout
                    # retried 3 times turned one minute of waiting into four,
                    # per phase, every phase.
                    if node.outputs.get("retryable") is False:
                        node.status = "failed"
                        log.error(
                            "DAGExecutionEngine: Node %s failed non-retryably (%s) — "
                            "not retrying",
                            node.id, node.outputs.get("error", "no error recorded"),
                        )
                        await self.save_node(node)
                        continue

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

        val_node = (
            nodes.get(val_node_id)
            or self._first_node_of_type(nodes, "validation")
            # A tool-only chain has no validation node; the tool node's
            # outputs (tool result, error) are the task's result.
            or self._first_node_of_type(nodes, "tool")
        )
        final_success = bool(nodes) and all(n.status == "completed" for n in nodes.values())
        final_output = val_node.outputs if (final_success and val_node) else {}

        # Promotion of the written file back into the workspace is the
        # spec's "promote" step; the workflow node can turn it off to leave
        # results quarantined in the worktree.
        promote = True
        if workflow_spec:
            promote = bool((workflow_spec.get("on_success") or {}).get("promote_to_workspace", True))

        # --- Fleet Harness: Output Merging and Resource Teardown ---
        import shutil
        if final_success and not promote:
            log.info("DAGExecutionEngine: Workflow node disables promote_to_workspace — leaving outputs in worktree.")
        if final_success and promote:
            log.info("DAGExecutionEngine: All nodes completed successfully. Merging outputs to working directory...")
            wri_node = nodes.get(wri_node_id) or self._first_node_of_type(nodes, "writing")
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

            # Tool nodes write files too (write_workspace_file runs against
            # the task's worktree via workspace_root_override), and until now
            # only writing-node outputs merged back — a tool-written file
            # stayed in the worktree and was destroyed with it at teardown.
            if Path(worktree_path).resolve() != Path(self.root_dir).resolve():
                for n in nodes.values():
                    if n.node_type != "tool" or n.status != "completed":
                        continue
                    tool_out = (n.outputs or {}).get("output")
                    tool_fp = tool_out.get("file_path") if isinstance(tool_out, dict) else None
                    if not tool_fp:
                        continue
                    src = Path(tool_fp) if Path(tool_fp).is_absolute() else Path(worktree_path) / tool_fp
                    if not src.is_file():
                        continue
                    try:
                        rel_path = os.path.relpath(src, worktree_path)
                        dest_path = Path(self.root_dir) / rel_path
                        if src.resolve() != dest_path.resolve():
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dest_path)
                            log.info("DAGExecutionEngine: Merged tool output %s back to working directory.", rel_path)
                    except Exception as merge_exc:
                        log.error("DAGExecutionEngine: Tool-output merge failed for %s: %s", tool_fp, merge_exc)

        # Release port allocation
        try:
            await router.release_port(task_id)
        except Exception as clean_exc:
            log.warning("DAGExecutionEngine: Failed to release port for %s: %s", task_id, clean_exc)

        # Cleanup Git Worktree folder on teardown
        if worktree_path and worktree_path != self.root_dir:
            try:
                from sdk.agent.async_shell import AsyncShellExecutor
                await AsyncShellExecutor.run_exec("git", ["worktree", "remove", "--force", worktree_path], cwd=self.root_dir)
                log.info("DAGExecutionEngine: Cleaned up Git Worktree folder %s", worktree_path)
            except Exception as wt_exc:
                log.warning("DAGExecutionEngine: Failed to remove Git Worktree: %s", wt_exc)

        return {
            "success": final_success,
            "output": final_output.get("output") or final_output,
            "error": val_node.outputs.get("error") if (val_node and not final_success) else None,
            "stdout": val_node.outputs.get("stdout", "") if val_node else "",
            "stderr": val_node.outputs.get("stderr", "") if val_node else "",
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
    ) -> bool:
        try:
            from sdk.agent.telemetry import LiveStatusReporter
            action_type = "search_workspace" if node.node_type == "research" else ("thinking" if node.node_type == "synthesis" else ("write_file" if node.node_type == "writing" else "run_command"))
            await LiveStatusReporter.broadcast_status(
                task_id=node.task_id,
                phase=f"DAG Node: {node.node_type.upper()}",
                status="in_progress",
                message=node.description,
                details={"action_type": action_type, "target": str(node.inputs.get("target_file", ""))}
            )
        except Exception:
            pass

        if node.node_type == "tool":
            # Dispatch a named tool through the live registry.
            #
            # Parameter names are normalised before the call because the
            # planner and the tool signatures disagree in practice -- it emits
            # "cron" where schedule_goal declares "cron_expr", so even once
            # dispatch existed the call would have failed on a missing required
            # argument. Unknown extras are dropped rather than passed through,
            # since a TypeError here reads to the caller as "the tool is
            # broken" rather than "the plan named the argument wrongly".
            import inspect as _inspect

            from sdk.agent.tools import ToolRegistry

            tool_name = node.inputs.get("tool", "")
            params = dict(node.inputs.get("tool_params") or {})

            entry = ToolRegistry._tools.get(str(tool_name).lower())
            if not entry:
                node.outputs = {
                    "success": False,
                    "error": f"tool {tool_name!r} is not registered",
                }
                return False

            # Governance, enforced rather than advisory.
            #
            # forbidden_actions is documented as an absolute kill-switch and had
            # never blocked anything, because the compiled packet never reached
            # execution. Affordances only constrain when a node actually
            # declares some -- an empty list means "unconstrained", not "deny
            # everything", so nodes that predate the tags keep working.
            governance = node.inputs.get("governance") or {}
            forbidden = {str(f).lower() for f in (governance.get("forbidden_actions") or [])}
            afforded = [str(a).lower() for a in (governance.get("tool_affordances") or [])]

            if str(tool_name).lower() in forbidden:
                node.outputs = {
                    "success": False,
                    "tool": tool_name,
                    "error": f"tool {tool_name!r} is in forbidden_actions for this goal",
                }
                log.warning("DAG tool node blocked by governance: %s", tool_name)
                return False

            if afforded and str(tool_name).lower() not in afforded:
                node.outputs = {
                    "success": False,
                    "tool": tool_name,
                    "error": (
                        f"tool {tool_name!r} is not in this goal's tool_affordances "
                        f"({', '.join(sorted(afforded)[:6])})"
                    ),
                }
                log.warning("DAG tool node not afforded: %s", tool_name)
                return False

            func = entry["func"]
            try:
                sig = _inspect.signature(func)
                accepted = set(sig.parameters)
            except (TypeError, ValueError):
                accepted = set(params)

            aliases = {
                "cron": "cron_expr",
                "schedule": "cron_expr",
                "goal": "goal_text",
                "text": "goal_text",
                "path": "file_path",
                "query_text": "query",
            }
            for wrong, right in aliases.items():
                if wrong in params and right in accepted and right not in params:
                    params[right] = params.pop(wrong)

            dropped = [k for k in params if k not in accepted]
            for k in dropped:
                params.pop(k)

            # A re-planned step often arrives with the tool named but its
            # arguments paraphrased away entirely. Calling through anyway
            # raised a bare TypeError from deep inside the call, which told
            # the log nothing about *which* plan step was malformed. Name the
            # missing arguments instead.
            try:
                required = [
                    name for name, p in sig.parameters.items()
                    if p.default is _inspect.Parameter.empty
                    and p.kind not in (
                        _inspect.Parameter.VAR_POSITIONAL,
                        _inspect.Parameter.VAR_KEYWORD,
                    )
                ]
            except Exception:
                required = []
            missing = [name for name in required if name not in params]
            if missing:
                node.outputs = {
                    "success": False,
                    "tool": tool_name,
                    "error": (
                        f"plan step for tool {tool_name!r} is missing required "
                        f"argument(s) {missing}; supplied: {sorted(params)}"
                    ),
                    # Deterministic: the same plan step will be missing the same
                    # argument on every attempt. Retrying it three times bought
                    # nothing and cost the goal its time budget -- measured on
                    # test_logic_bug, which exhausted 6.4 of its 5.0 minutes
                    # re-running an identical empty code.propose_patch call.
                    "retryable": False,
                }
                log.error(
                    "DAGExecutionEngine: tool %s plan step missing required arg(s) %s "
                    "(supplied: %s)", tool_name, missing, sorted(params),
                )
                return False

            try:
                # Workspace tools must act in THIS task's workspace (its
                # worktree, or the engine root_dir), not the install
                # checkout — see workspace_root_override in tools.py.
                from sdk.agent.tools import workspace_root_override
                with workspace_root_override(worktree_path or self.root_dir):
                    result = func(**params)
                    if _inspect.isawaitable(result):
                        result = await result
            except Exception as exc:
                log.exception("DAG tool node %s failed", tool_name)
                node.outputs = {
                    "success": False,
                    "tool": tool_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                return False

            # Tools signal failure through either convention: {"success":
            # False} or {"status": "failed"/"error"}. Only the first was
            # checked, so a run_workspace_command returning {"status":
            # "failed", "stderr": ...} completed its node green and the
            # whole goal reported success off a failed command.
            ok = True
            if isinstance(result, dict):
                if result.get("success") is False:
                    ok = False
                elif str(result.get("status", "")).strip().lower() in ("failed", "error", "failure"):
                    ok = False

            node.outputs = {
                "success": ok,
                "tool": tool_name,
                "dropped_params": dropped,
                "output": result,
            }
            log.info(
                "DAGExecutionEngine: tool %s -> %s", tool_name, str(result)[:200]
            )
            return ok

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
            # Synthesis Logic: Call LLM to design and synthesize code for the solution
            problem = node.inputs.get("problem", "")
            if not problem:
                research_node = self._first_node_of_type(nodes, "research")
                if research_node:
                    problem = research_node.inputs.get("problem", "")

            error_feedback = node.inputs.get("error_feedback", "")
            target_file = node.inputs.get("target_file", "scratch/output.py")

            research_node = self._first_node_of_type(nodes, "research")
            sources = research_node.outputs.get("sources", []) if research_node else []

            prompt = (
                f"Write complete, valid, working Python code for the following task:\n\"{problem}\"\n\n"
                f"Target file path: {target_file}\n"
                f"Reference sources: {sources}\n\n"
            )

            # A "fix X in <file>" goal is meaningless if the model never sees
            # the file: synthesis used to regenerate every target from
            # scratch, so fixing a bug meant guessing what the file contained
            # and repair goals only ever succeeded by luck. Show the current
            # content and demand a full updated file that preserves the rest.
            try:
                _existing_target = (
                    Path(target_file) if Path(target_file).is_absolute()
                    else Path(worktree_path) / target_file
                )
                if _existing_target.is_file():
                    _current = _existing_target.read_text(encoding="utf-8", errors="replace")
                    if len(_current) > 6000:
                        _current = _current[:6000] + "\n# ... (truncated)"
                    if _current.strip():
                        prompt += (
                            f"CURRENT CONTENT of {target_file}:\n"
                            f"```python\n{_current}\n```\n"
                            "Your output must be the COMPLETE updated file. Apply only the "
                            "changes the task requires and preserve all other existing code "
                            "and comments exactly as they are.\n\n"
                        )
            except Exception:
                pass

            # Inject standardized CodeRegistry template if target is a skill file
            try:
                from sdk.agent.code_registry import CodeRegistry
                if "skill" in target_file.lower() or "skills/" in target_file.lower() or "skill" in problem.lower():
                    skill_template = await CodeRegistry.get("tala.code.template.python.skill")
                    if skill_template:
                        prompt += f"Standard Boilerplate Template Structure:\n{skill_template}\n\n"
            except Exception:
                pass

            prompt += (
                "CRITICAL INSTRUCTIONS:\n"
                "1. Output ONLY the raw Python source code inside a ```python ``` fenced code block.\n"
                "2. Do NOT output thinking logs, tool call tags, conversational preambles, or markdown explanations.\n"
                "3. Ensure all imports (such as @register_tool from sdk.agent.tools) and function signatures match the prompt specifications exactly.\n"
            )
            if error_feedback:
                prompt += f"\n\nCRITICAL FIX: The previous attempt failed validation with error:\n{error_feedback}\nModify the code to resolve this exact error."

            # The output contract goes last, and stays intact when a persona is
            # present. A persona supplies character and priorities; it must not
            # be able to override "emit only code", because the writing node
            # downstream parses this response as source and a chatty answer
            # fails validation rather than reading as a refusal.
            output_contract = (
                "Output ONLY valid Python code inside a single ```python ``` code block. "
                "Never write prose, thinking logs, or markdown outside the code block."
            )
            persona_prompt = getattr(self, "_persona_prompt", "")
            if persona_prompt:
                system_prompt = (
                    f"{persona_prompt}\n\n"
                    f"For this task you are working as an expert Python software engineer. {output_contract}"
                )
            else:
                system_prompt = f"You are an expert Python software engineer. {output_contract}"

            log.info("DAGExecutionEngine: Synthesis node initiating LLM call for target %s...", target_file)
            code = ""
            try:
                from python.harness.server import handle_inference_generate

                async def _call_llm():
                    p = {
                        "prompt": prompt,
                        "stream": False,
                        "raw_prompt": True,
                        "system_prompt": system_prompt,
                        "max_tokens": 3072,
                    }
                    return await handle_inference_generate(p, ws=None, req_id="dag-synthesis")

                # Sized from measurement, not habit: a chat turn against the
                # local 8B on this hardware runs 60-160s, so the old 60s ceiling
                # meant synthesis could essentially never finish -- it failed on
                # the clock every time and the stale-file fallback above dressed
                # that up as success. 300s leaves headroom over the slowest
                # measured turn.
                res_str = await asyncio.wait_for(_call_llm(), timeout=SYNTHESIS_TIMEOUT_SECONDS)
                res_dict = json.loads(res_str)

                if isinstance(res_dict, dict) and "result" in res_dict and isinstance(res_dict["result"], dict):
                    text = res_dict["result"].get("text", "")
                elif isinstance(res_dict, dict) and "error" in res_dict:
                    err_msg = res_dict["error"].get("message") if isinstance(res_dict["error"], dict) else str(res_dict["error"])
                    raise RuntimeError(f"Inference error: {err_msg}")
                else:
                    text = ""

                log.info("DAGExecutionEngine: Synthesis LLM response received (%d bytes). Parsing Python code...", len(text))

                if text:
                    # Strategy 1: Direct extraction from markdown fenced code block
                    m = re.search(r"```(?:python|py)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL | re.IGNORECASE)
                    if m:
                        code = m.group(1).strip()

                    # Strategy 2: Strip thinking tags and search again
                    if not code:
                        cleaned_text = re.sub(r"(?i)<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL)
                        cleaned_text = re.sub(r"<\/?call:[^>]+>", "", cleaned_text)
                        cleaned_text = re.sub(r"\*\(Executing.*?\)\*", "", cleaned_text)
                        
                        m2 = re.search(r"```(?:python|py)?\s*\n?(.*?)\n?\s*```", cleaned_text, re.DOTALL | re.IGNORECASE)
                        if m2:
                            code = m2.group(1).strip()
                        else:
                            # Strategy 3: Extract top-level python statements line by line
                            code_lines = []
                            for line in cleaned_text.splitlines():
                                stripped = line.strip()
                                if any(stripped.startswith(kw) for kw in ("import ", "from ", "def ", "async def ", "class ", "@", "return ", "if ", "for ", "while ", "try:", "except", "#")):
                                    code_lines.append(line)
                                elif code_lines and (line.startswith("    ") or line.startswith("\t") or stripped == ""):
                                    code_lines.append(line)
                            code = "\n".join(code_lines).strip()

                    # Validate syntax via AST
                    if code:
                        import ast
                        try:
                            ast.parse(code)
                            log.info("DAGExecutionEngine: Synthesis successfully extracted %d bytes of valid Python code.", len(code))
                        except SyntaxError as se:
                            log.warning("DAGExecutionEngine: Extracted synthesis code failed AST validation: %s. Code snippet: %s", se, repr(code[:200]))
                            lines_f = [l for l in code.splitlines() if any(l.strip().startswith(kw) for kw in ("import ", "from ", "def ", "async def ", "class ", "@", "return ", "if ", "for ", "while ", "try:", "except", "#")) or l.startswith(" ") or l.startswith("\t")]
                            candidate = "\n".join(lines_f).strip()
                            try:
                                ast.parse(candidate)
                                code = candidate
                                log.info("DAGExecutionEngine: Synthesis line-filtered fallback produced %d bytes of valid Python.", len(code))
                            except SyntaxError as se2:
                                log.warning("DAGExecutionEngine: Fallback candidate also failed AST validation: %s", se2)
                                code = ""

            except asyncio.TimeoutError:
                # No "reuse whatever is already at target_file" fallback here.
                # That fallback read back a stale scratch/output.py from some
                # earlier run, called it this phase's synthesis, wrote the
                # identical bytes out again, validated them, and reported the
                # phase COMPLETED SUCCESSFULLY -- so a goal whose model never
                # answered still advanced through its phases and told the user
                # it had done the work. A timeout is a failure; say so.
                #
                # retryable=False: the model that did not answer in 300s will
                # not answer in the next 300s either, and the retry loop would
                # spend 20 minutes proving it.
                log.warning(
                    "DAGExecutionEngine: LLM synthesis timed out after %ss — "
                    "failing the node (no code was produced).",
                    SYNTHESIS_TIMEOUT_SECONDS,
                )
                node.outputs = {
                    "error": f"LLM synthesis timed out after {SYNTHESIS_TIMEOUT_SECONDS}s; no code produced.",
                    "retryable": False,
                }
                return False
            except Exception as exc:
                log.warning("DAGExecutionEngine: LLM synthesis failed: %s", exc)
                node.outputs = {"error": f"LLM synthesis failed: {exc}"}
                return False

            if not code:
                # Same reasoning as the timeout branch above: an empty or
                # unparseable model response is a failed synthesis, not a
                # licence to re-adopt the previous run's file as this run's
                # output.
                log.warning("DAGExecutionEngine: LLM synthesis returned no valid Python code.")
                node.outputs = {"error": "LLM synthesis returned no valid Python code."}
                return False

            node.outputs = {
                "code": code,
                "strategy": "Generated code draft ready to write.",
            }
            return True


        elif node.node_type == "writing":
            # Writing Logic: Write synthesized code to target files.
            #
            # Resolved from this node's own dependency rather than "the first
            # synthesis node in the graph". With one target the two are the
            # same; with several, every writer would take the first file's code
            # and write it to every path -- so a plan that touches three files
            # would produce three copies of one of them.
            syn_node = next(
                (n for n in nodes.values()
                 if n.id in (node.dependencies or []) and n.node_type == "synthesis"),
                None,
            ) or self._first_node_of_type(nodes, "synthesis")
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
            # Validation Logic: Compile written code and run tests directly on the host.
            target_file = node.inputs.get("target_file", "scratch/output.py")
            # Always resolve to an absolute path
            target_path = (
                Path(target_file) if Path(target_file).is_absolute()
                else Path(worktree_path) / target_file
            )
            test_file = node.inputs.get("test_file")

            if not target_path.exists():
                node.outputs = {"error": f"Target file does not exist: {target_path}"}
                return False

            # Which checks run is part of the workflow node's step config
            # ("checks" lands in inputs via the spec). Absent a spec, all of
            # them run — the spec can narrow the gate, but the default stays
            # the strictest behavior.
            checks_cfg = node.inputs.get("checks")
            if isinstance(checks_cfg, (list, tuple)) and checks_cfg:
                checks = {str(c).strip().lower() for c in checks_cfg}
            else:
                checks = {"py_compile", "import_probe", "register_assert", "pytest"}

            # 1. Syntax / compilation check
            if target_path.suffix.lower() == ".py" and "py_compile" in checks:
                try:
                    chk = await AsyncShellExecutor.run(
                        f'python -m py_compile "{target_path}"',
                        cwd=str(target_path.parent),
                        timeout=20.0,
                    )
                except Exception as exc:
                    node.outputs = {"error": f"Compilation check failed: {exc}"}
                    return False

                if not chk["success"]:
                    node.outputs = {
                        "success": False,
                        "error": f"SyntaxError:\n{chk.get('stderr') or chk.get('stdout')}",
                        "stdout": chk.get("stdout", ""),
                        "stderr": chk.get("stderr", ""),
                    }
                    return False

            # 1b. Import / smoke check.
            #
            # py_compile only proves the file PARSES. It says nothing about
            # whether the code works, and in practice it was the only check that
            # ever ran -- `test_file` comes from a structured context key that
            # nothing populates from natural language, so the pytest branch below
            # is almost always skipped. Every piece of code Tala has ever
            # written was accepted on "it is syntactically valid Python".
            #
            # Actually importing it catches the failures small models really
            # make: hallucinated imports, undefined names, bad decorator usage,
            # module-level typos. For a skill there is a stronger contract
            # available -- it must register a tool -- so that is asserted too.
            #
            # Runs in a subprocess: importing generated code executes its
            # module-level statements, which must not happen inside the harness.
            if target_path.suffix.lower() == ".py" and "import_probe" in checks:
                _src_text = target_path.read_text(encoding="utf-8", errors="replace")
                _is_skill = "register_assert" in checks and "register_tool" in _src_text

                # Enforce the contract the GOAL states, not just the one the
                # generated file happens to declare.
                #
                # The registration assertion only fired when the written file
                # already contained @register_tool -- so code that ignored the
                # requirement outright sailed through: a goal asking for "a
                # skill that registers a tool named reverse_words" was
                # satisfied by a file containing a factorial function, which
                # compiled and imported cleanly and was reported as success
                # (confirmed live 2026-08-11). If the goal names the tool it
                # wants, that name must actually be in the registry after the
                # module is imported.
                _required_tool = None
                if "register_assert" in checks:
                    _m = re.search(
                        r'(?:register(?:s|ing)?|expose[sd]?|provide[sd]?|call)\s+(?:a\s+)?tool\s+'
                        r'(?:named|called)\s+[`"\']?([A-Za-z_][A-Za-z0-9_]*)',
                        str(node.inputs.get("problem") or ""),
                        re.IGNORECASE,
                    )
                    if _m:
                        _required_tool = _m.group(1)
                _probe = (
                    "import importlib.util,sys;"
                    f"sys.path.insert(0,r'{self.root_dir or '.'}');"
                    f"spec=importlib.util.spec_from_file_location('_tala_probe',r'{target_path}');"
                    "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                )
                if _is_skill:
                    _probe += (
                        "from sdk.agent.tools import ToolRegistry;"
                        "assert ToolRegistry._tools, 'code declares @register_tool but registered nothing';"
                    )
                if _required_tool:
                    _probe += (
                        "from sdk.agent.tools import ToolRegistry as _TR;"
                        f"assert '{_required_tool}'.lower() in _TR._tools, "
                        f"\\\"the goal requires a registered tool named '{_required_tool}', \\\""
                        f"\\\" but importing this file registered: \\\" + (', '.join(sorted(_TR._tools)[:12]) or 'nothing');"
                    )
                _probe += "print('IMPORT_OK')"

                try:
                    imp = await AsyncShellExecutor.run(
                        f'python -c "{_probe}"',
                        cwd=str(self.root_dir or target_path.parent),
                        timeout=45.0,
                    )
                except Exception as exc:
                    node.outputs = {"success": False, "error": f"Import check could not run: {exc}"}
                    return False

                if "IMPORT_OK" not in (imp.get("stdout") or ""):
                    node.outputs = {
                        "success": False,
                        "error": (
                            "Code parses but fails to import/run:\n"
                            + (imp.get("stderr") or imp.get("stdout") or "no output")
                        ),
                        "stdout": imp.get("stdout", ""),
                        "stderr": imp.get("stderr", ""),
                    }
                    log.warning(
                        "DAG validation: %s compiled but failed import check", target_file
                    )
                    return False

            # 2. Test suite run
            if test_file and "pytest" in checks:
                test_path = (
                    Path(test_file) if Path(test_file).is_absolute()
                    else Path(worktree_path) / test_file
                )
                try:
                    test_res = await AsyncShellExecutor.run(
                        f'python -m pytest "{test_path}" -v',
                        cwd=str(Path(worktree_path)),
                        timeout=60.0,
                    )
                except Exception as exc:
                    node.outputs = {"error": f"Test suite execution failed: {exc}"}
                    return False

                if not test_res.get("success", False):
                    node.outputs = {
                        "success": False,
                        "error": f"Test suite failure:\n{test_res.get('stdout') or test_res.get('stderr')}",
                        "stdout": test_res.get("stdout", ""),
                        "stderr": test_res.get("stderr", ""),
                    }
                    return False

            # Regression: this used to unconditionally claim "test verification
            # passed successfully" even when test_file was never set (the normal
            # case for a freeform goal -- test_file only ever comes from a
            # structured context field nothing populates from natural language,
            # never from prose). A goal that explicitly asked for tests to be
            # written and run could report full success with zero tests ever
            # having executed. The message must say plainly whether tests ran.
            if test_file and "pytest" in checks:
                node.outputs = {"success": True, "output": "Compilation, import check, and test suite verification passed successfully."}
            else:
                # The message must only claim checks that actually ran — a
                # workflow-spec edit that narrows `checks` must be visible
                # here, not papered over by the default wording.
                if {"py_compile", "import_probe"} <= checks:
                    prefix = (
                        "Compilation and import check passed -- the code loads and runs at "
                        "module level."
                    )
                else:
                    ran = sorted(checks & {"py_compile", "import_probe"})
                    prefix = (
                        f"Checks passed: {', '.join(ran)} (narrowed by workflow spec)."
                        if ran else
                        "No code checks ran -- all disabled by the workflow spec."
                    )
                if test_file:
                    tail = (
                        " A test file was specified but the pytest check is disabled "
                        "in the workflow spec, so no automated tests were run."
                    )
                else:
                    tail = " No test file was specified, so no automated tests were run."
                node.outputs = {
                    "success": True,
                    "output": prefix + tail,
                    "tests_ran": False,
                }
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

            # Heal behavior is part of the workflow node's contract: the spec's
            # on_failure block can redirect the error feedback or keep failed
            # output in place for inspection. Defaults reproduce the built-in
            # behavior exactly.
            on_failure = {}
            spec = getattr(self, "_active_workflow_spec", None)
            if isinstance(spec, dict):
                on_failure = spec.get("on_failure") or {}
            do_rollback = bool(on_failure.get("rollback_written_files", True))
            feedback_to = str(on_failure.get("feedback_to") or "synthesis")
            reset_steps = on_failure.get("reset_steps")
            if not isinstance(reset_steps, list) or not reset_steps:
                reset_steps = ["writing"]

            # 1. Rollback file writes using backups
            if not do_rollback:
                backups = {}
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

            # 2. Inject validation error feedback into the feedback target
            # (the synthesis step unless the workflow node redirects it).
            def _resolve(step_or_type: str) -> GraphNode | None:
                return nodes.get(f"{step_or_type}-{node.task_id}") or self._first_node_of_type(nodes, step_or_type)

            syn_node = _resolve(feedback_to)
            if syn_node:
                error_msg = node.outputs.get("error") or "Unknown validation error"
                syn_node.inputs["error_feedback"] = error_msg
                # Reset the feedback target so the chain re-runs from it
                syn_node.status = "pending"
                syn_node.retry_count += 1
                await self.save_node(syn_node)

            for step_name in reset_steps:
                reset_node = _resolve(str(step_name))
                if reset_node and reset_node is not syn_node and reset_node is not node:
                    reset_node.status = "pending"
                    reset_node.retry_count += 1
                    await self.save_node(reset_node)

            # Reset validation node itself
            node.status = "pending"

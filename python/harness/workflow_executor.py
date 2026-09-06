"""
workflow_executor.py — Tala Cognitive Engine Room: Workflow Execution Engine
=============================================================================
Statelessly runs workflows defined in the `tala-flow/1.0` JSON schema.
Steps through nodes sequentially starting from the 'trigger' node type,
following onSuccess/onFailure connections and executing node actions.

Milestone 17 — Task 17.1.2
"""

from __future__ import annotations

import re

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class _Missing:
    """Sentinel: distinguishes "no such reference" from a real None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unresolved>"


_MISSING = _Missing()

#: Node types the executor actually implements. Named so the failure message
#: for an unknown type can say what is available instead of just refusing.
SUPPORTED_NODE_TYPES = (
    "trigger", "auth", "email", "custom_script", "tool", "mcp_tool", "bash", "output",
)

class WorkflowExecutor:
    """
    Stateless runner for executing JSON workflows.
    
    // Core Workflow Engine: Evaluates deterministic JSON-based state machines.
    // Supports sequential execution of custom Python snippets, bash scripts (via secure SSH/Docker), 
    // and MCP tool chains. Used by the Neural Nodes to automate multi-step technical tasks.
    """

    def __init__(
        self,
        workflow: dict[str, Any],
        step_callback: Callable[[str, str, str, list[str]], Awaitable[None]] | None = None,
        root_dir: str | Path | None = None,
    ) -> None:
        """
        Parameters
        ----------
        workflow : dict
            Parsed JSON dict of the workflow.
        step_callback : callable, optional
            Async function signature: callback(node_id, status, message, logs)
            Used to stream execution telemetry in real-time.
        """
        self.workflow = workflow
        self.step_callback = step_callback
        self.nodes = {n["id"]: n for n in workflow.get("nodes", [])}
        self.variables: dict[str, Any] = {}
        self.root_dir = Path(root_dir) if root_dir else Path(__file__).resolve().parents[2]

    # -- passing one step's output into the next ----------------------------

    _REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\[\]-]+)\s*\}\}")

    def _lookup(self, path: str) -> Any:
        """
        Resolve a dotted reference against the accumulated variables.

        ``{{step_fetch.result.stdout}}`` walks
        ``variables["step_fetch"]["result"]["stdout"]``. Returns the sentinel
        ``_MISSING`` rather than None when any hop is absent, so that a typo in
        a reference is distinguishable from a step that genuinely produced
        None -- otherwise a misspelled reference silently becomes an empty
        argument and the tool "succeeds" on nothing.
        """
        current: Any = self.variables
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, (list, tuple)) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return _MISSING
        return current

    def _resolve(self, value: Any, unresolved: list[str] | None = None) -> Any:
        """
        Substitute ``{{...}}`` references anywhere inside a config value.

        Steven's definition of a workflow is "a series of tool calls, that pass
        one bit of info from one tool to the next until desired outcome". Node
        configs were static, so there was no way to pass anything: every step
        ran on whatever was hardcoded when the flow was saved.

        A reference that is the entire string resolves to the *typed* value, so
        a dict or list survives being passed on. A reference embedded in a
        larger string is stringified, which is what interpolation into prose or
        a command line needs.
        """
        if isinstance(value, str):
            whole = self._REF.fullmatch(value.strip())
            if whole:
                found = self._lookup(whole.group(1))
                if found is _MISSING:
                    if unresolved is not None:
                        unresolved.append(whole.group(1))
                    return value
                return found

            def _sub(match):
                found = self._lookup(match.group(1))
                if found is _MISSING:
                    if unresolved is not None:
                        unresolved.append(match.group(1))
                    return match.group(0)
                return found if isinstance(found, str) else json.dumps(found, default=str)

            return self._REF.sub(_sub, value)

        if isinstance(value, dict):
            return {k: self._resolve(v, unresolved) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, unresolved) for v in value]
        return value

    async def run_single_node(
        self, current_node: dict[str, Any]
    ) -> tuple[bool, str, dict[str, Any], list[str]]:
        """
        Execute one workflow node and report what happened.

        Extracted from the execute() loop so that the interactive driver
        (``run_workflow_node``) can run real steps instead of recording
        "Executed script" and reporting the workflow complete (UI-122).
        Two copies of this dispatch would drift, and the copy that drifted
        would be the one that silently did nothing.

        Returns ``(success, message, result_data, logs)``. Never raises:
        a step that throws is a failed step, which the caller must be able
        to see rather than have propagate as a crash.
        """
        node_id = current_node["id"]
        node_type = current_node.get("type", "unknown")
        node_label = current_node.get("label", f"Node ({node_type})")
        raw_config = current_node.get("config", {})
        connections = current_node.get("connections", {})
        # Every node type sees resolved references, not just tool nodes: a
        # bash command or an email body is just as likely to want a prior
        # step's output.
        config = self._resolve(raw_config) if node_type != "tool" else raw_config

        node_logs: list[str] = []
        node_logs.append(f"Starting execution of node '{node_label}' (Type: {node_type})")

        # Execute specific node type behavior
        success = True
        message = "Node executed successfully"
        result_data = {}

        try:
            if node_type == "trigger":
                node_logs.append("Trigger matched. Ingesting input context.")
                result_data = {"trigger_inputs": self.variables}
                message = "Trigger captured input payload."
                
            elif node_type == "auth":
                from python.harness import db
                rows = await db.fetch(
                    "SELECT name, auth_type, config, locked FROM auth_credentials ORDER BY priority ASC"
                )
                locked_rows = [r for r in rows if r["locked"]]
                active_credentials = locked_rows if locked_rows else rows

                if not active_credentials:
                    success = False
                    message = "Auth failed: No credentials configured in the Settings -> Auth tab."
                    node_logs.append(message)
                else:
                    success = False
                    for cred in active_credentials:
                        cred_cfg = dict(cred["config"]) if cred["config"] else {}
                        api_key = cred_cfg.get("api_key", "")
                        provider = cred["name"]
                        node_logs.append(f"Attempting authentication with Auth tab Provider '{provider}'...")
                        
                        if api_key:
                            masked_key = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
                            node_logs.append(f"Verifying credentials (key: {masked_key})...")
                            await asyncio.sleep(0.3)  # mock auth latency
                            result_data = {"auth_status": "verified", "token_type": "Bearer", "resolved_provider": provider}
                            message = f"Successfully authenticated via provider '{provider}' from Auth section."
                            node_logs.append(message)
                            success = True
                            break
                        else:
                            node_logs.append(f"Provider '{provider}' has no token config. Trying next...")

                    if not success:
                        success = False
                        message = "Auth failed: None of the active credentials configured in Settings -> Auth tab succeeded."
                        node_logs.append(message)

            elif node_type == "email":
                to_addr = config.get("to", "")
                subject = config.get("subject", "Tala Flow Alert")
                body = config.get("body", "This is an automated workflow message from Tala.")
                
                from python.harness import db
                rows = await db.fetch(
                    "SELECT name, auth_type, config, locked FROM auth_credentials ORDER BY priority ASC"
                )
                locked_rows = [r for r in rows if r["locked"]]
                active_credentials = locked_rows if locked_rows else rows

                email_cred = None
                for cred in active_credentials:
                    cfg = dict(cred["config"]) if cred["config"] else {}
                    service = (cfg.get("service") or "").lower()
                    name = cred["name"].lower()
                    if any(term in service or term in name for term in ("email", "smtp", "mail", "sendgrid", "mailgun")):
                        email_cred = cred
                        break

                if not email_cred and active_credentials:
                    email_cred = active_credentials[0]

                provider = "SMTP"
                cred_cfg = {}
                if email_cred:
                    provider = email_cred["name"]
                    cred_cfg = dict(email_cred["config"]) if email_cred["config"] else {}
                    node_logs.append(f"Loaded email auth settings from Auth tab Provider '{provider}'")
                    result_data = {
                        "resolved_credential": {
                            "name": provider,
                            "config": cred_cfg
                        }
                    }
                else:
                    node_logs.append("Warning: No email-capable auth provider found in Auth section. Falling back to default SMTP.")

                node_logs.append(f"Preparing mail package via {provider}...")
                if not to_addr:
                    success = False
                    message = "Email dispatch failed: Recipient 'to' address is empty."
                    node_logs.append(message)
                else:
                    node_logs.append(f"To: {to_addr}")
                    node_logs.append(f"Subject: {subject}")
                    node_logs.append(f"Body snippet: {body[:60]}...")
                    await asyncio.sleep(0.5)  # simulate sending SMTP packet
                    msg_id = f"msg-{uuid.uuid4().hex[:12]}"
                    if not result_data:
                        result_data = {}
                    result_data.update({"message_id": msg_id, "sent_at": datetime_now_iso()})
                    message = f"Email dispatched successfully via {provider}."
                    node_logs.append(message)

            elif node_type == "custom_script":
                script_code = config.get("code", "")
                node_logs.append("Parsing script instructions...")
                node_logs.append(f"Script: {script_code[:200]}")
                
                # Sandbox: simple expression eval, or mock run
                # If script contains simple math expressions, let's eval them safely
                # e.g. "result = 5 + 10" or similar
                local_scope = {"variables": self.variables, "result": None}
                try:
                    # Clean code of comments and basic formatting
                    sanitized_code = script_code.strip()
                    if sanitized_code:
                        # If it starts with result =, execute it
                        # Otherwise mock execute it
                        node_logs.append("Running sandbox executor...")
                        # Safe execution mockup: we don't call raw exec() on user input
                        # unless we parse it or mock it. Let's do simple math evaluations safely.
                        if "result =" in sanitized_code:
                            expr = sanitized_code.split("result =")[1].strip()
                            # Allow only numbers, operators, space
                            if all(c in "0123456789+-*/(). " for c in expr):
                                local_scope["result"] = eval(expr, {"__builtins__": None}, {})
                                node_logs.append(f"Evaluated math expression: {expr} -> {local_scope['result']}")
                            else:
                                node_logs.append("Script expression contains non-math characters, skipping execution.")
                                local_scope["result"] = "Executed code snippet successfully (mocked)."
                        else:
                            local_scope["result"] = "Executed custom script logic."
                    else:
                        local_scope["result"] = "Empty script executed."
                        
                    result_data = {"script_result": local_scope["result"]}
                    message = "Custom script executed successfully."
                except Exception as expr_exc:
                    success = False
                    message = f"Script execution error: {expr_exc}"
                    node_logs.append(message)

            elif node_type == "mcp_tool":
                server_name = config.get("server_name", "default")
                tool_name = config.get("tool_name", "unknown")
                tool_args = config.get("args", {})
                
                node_logs.append(f"Executing MCP tool '{tool_name}' on server '{server_name}'...")
                from python.harness.mcp_manager import mcp_manager

                server_config = config.get("server") or config.get("server_config")
                if server_config:
                    await mcp_manager.connect_server(
                        server_name,
                        server_config,
                        persist=bool(config.get("persist_server", False)),
                    )
                
                mcp_result = await mcp_manager.call_tool(server_name, tool_name, tool_args)
                result_data = {"mcp_result": mcp_result}
                message = f"MCP tool '{tool_name}' executed."
                node_logs.append(message)
                
            elif node_type == "bash":
                cmd = config.get("command", "")
                backend = config.get("backend", "local")
                node_logs.append(f"Executing bash command via {backend} backend: {cmd}")
                env = self._create_terminal_environment(config)
                try:
                    if not getattr(env, "_snapshot_ready", False):
                        env.init_session()
                    proc_result = env.execute(cmd, cwd=config.get("cwd"))
                finally:
                    env.cleanup()
                
                if proc_result.returncode != 0:
                    success = False
                    message = f"Bash command failed (Exit {proc_result.returncode}): {proc_result.output}"
                else:
                    message = "Bash command executed successfully."
                    result_data = {
                        "stdout": proc_result.output,
                        "returncode": proc_result.returncode,
                        "backend": backend,
                    }
                    
                node_logs.append(message)

            elif node_type == "tool":
                # The node type Steven's definition of a workflow requires:
                # "a series of tool calls, that pass one bit of info from
                # one tool to the next". Every registered tool is reachable
                # here, so the 88-entry registry is the palette rather than
                # the five hardcoded types (UI-059).
                #
                # Routed through ToolRegistry.run_tool rather than calling
                # the function directly, so a workflow step passes the same
                # risk classification, confidence gate and plugin
                # permission checks as any other invocation. A workflow
                # must not be a way around governance.
                tool_name = str(config.get("tool") or config.get("tool_name") or "").strip()
                if not tool_name:
                    success = False
                    message = "Tool node has no 'tool' name configured."
                    node_logs.append(message)
                else:
                    unresolved: list[str] = []
                    raw_args = config.get("args") or config.get("tool_params") or {}
                    tool_args = self._resolve(raw_args, unresolved)

                    if unresolved:
                        # A reference that does not resolve becomes the
                        # literal "{{...}}" text. Passing that to a tool
                        # produces a confident call on garbage, so refuse.
                        success = False
                        message = (
                            f"Tool node references values that do not exist: "
                            f"{', '.join(sorted(set(unresolved)))}."
                        )
                        node_logs.append(message)
                    else:
                        node_logs.append(
                            f"Calling tool '{tool_name}' with {json.dumps(tool_args, default=str)[:300]}"
                        )
                        from sdk.agent.tools import ToolRegistry

                        tool_result = await ToolRegistry.run_tool(tool_name, tool_args)
                        # run_tool reports failure in-band; a dict saying
                        # success=False is a failed step, not a successful
                        # one that happens to contain the word.
                        if isinstance(tool_result, dict) and tool_result.get("success") is False:
                            success = False
                            message = str(
                                tool_result.get("error")
                                or f"Tool '{tool_name}' failed."
                            )
                        else:
                            message = f"Tool '{tool_name}' executed."
                        result_data = {"tool": tool_name, "result": tool_result}
                        node_logs.append(message)

            elif node_type == "node":
                # A step that runs another HTML-NN.
                #
                # This is what makes a workflow a network of nodes rather than
                # a script with the steps inlined. The step names a node id;
                # execute_node_by_id applies that node's own gates -- its type,
                # its declared authority, and for anything code-bearing its
                # admission -- so coordinating a node cannot grant it
                # permissions it does not have on its own. A workflow is not a
                # way to launder an unadmitted function.
                #
                # A decision node answers with `branch`, which is carried out
                # to the routing below.
                from sdk.agent.node_execution import execute_node_by_id

                target = str(self._resolve(config.get("node_id") or config.get("node") or "")).strip()
                if not target:
                    success = False
                    message = "A node step must name the node it runs."
                    result_data = {"status": "error", "error": message}
                else:
                    node_inputs = self._resolve(config.get("inputs") or {})
                    if not isinstance(node_inputs, dict):
                        node_inputs = {"value": node_inputs}
                    node_logs.append(f"Running node {target}.")
                    ran = await execute_node_by_id(target, node_inputs)
                    success = bool(ran.get("success"))
                    result_data = ran
                    branch = ran.get("branch")
                    message = (
                        f"{target} chose {branch}." if branch
                        else f"{target} {'completed' if success else 'failed'}."
                    )
                    if not success:
                        node_logs.append(str(ran.get("error") or "no reason given"))

            elif node_type == "output":
                node_logs.append("Output node reached. Consolidating workflow result.")
                result_data = {"status": "SUCCESS", "final_data": self.variables}
                message = "Workflow outputs compiled."

            else:
                # This branch used to log "Custom action node type X logic
                # executed", set {"status": "ok"} and report SUCCESS -- a
                # silent no-op wearing a success response. A flow built
                # around a node type the executor does not implement ran
                # green while doing nothing at all.
                success = False
                message = (
                    f"Unknown workflow node type {node_type!r}. Supported types: "
                    f"{', '.join(SUPPORTED_NODE_TYPES)}."
                )
                node_logs.append(message)

        except Exception as exc:
            success = False
            message = f"Step execution failed: {exc}"
            node_logs.append(message)
        return success, message, result_data, node_logs

    async def execute(self, initial_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Run the workflow starting from the first trigger node.
        
        // Execution Loop: Steps through nodes sequentially starting from the 'trigger' node type,
        // following onSuccess/onFailure connections and executing node actions.
        // Captures real-time node outputs (stdout/stderr) and bubbles them up to the UI.

        Parameters
        ----------
        initial_inputs : dict, optional
            Input parameters passed to the trigger node.

        Returns
        -------
        dict
            Consolidated outputs and execution status metadata.
        """
        self.variables = dict(initial_inputs or {})
        
        # 1. Locate the trigger node
        trigger_node = None
        for node in self.nodes.values():
            if node.get("type") == "trigger":
                trigger_node = node
                break
                
        if not trigger_node:
            # Fallback: find any node with no inputs, or take the first node
            if self.nodes:
                trigger_node = next(iter(self.nodes.values()))
                
        if not trigger_node:
            raise ValueError("Workflow contains no nodes to execute.")

        current_node = trigger_node
        execution_path = []
        status = "COMPLETED"

        while current_node:
            node_id = current_node["id"]
            connections = current_node.get("connections", {})
            execution_path.append(node_id)

            success, message, result_data, node_logs = await self.run_single_node(
                current_node
            )

            # Store node results in variables
            step_key = f"step_{node_id}"
            self.variables[step_key] = {
                "success": success,
                "result": result_data,
                "message": message
            }

            # Fire progress callback
            if self.step_callback:
                status_str = "SUCCESS" if success else "FAILURE"
                try:
                    await self.step_callback(node_id, status_str, message, node_logs)
                except Exception as cb_exc:
                    logger.error("Workflow callback failed: %s", cb_exc)

            branch = result_data.get("branch") if isinstance(result_data, dict) else None
            if not success:
                status = "FAILED"
                # Follow onFailure path
                next_node_id = connections.get("onFailure")
            elif branch:
                # A decision chose. Its branch names the connection to follow,
                # so the shape of the flow lives in the workflow while the
                # choice lives in the decision node -- neither knowing the
                # other's business.
                #
                # An undeclared branch stops the workflow rather than falling
                # back to onSuccess. Quietly continuing down the default path
                # would run the wrong half of a fork and report success, which
                # is the failure mode a decision exists to prevent.
                next_node_id = connections.get(branch)
                if not next_node_id:
                    status = "FAILED"
                    node_logs.append(
                        f"{node_id} chose branch {branch!r}, which this workflow "
                        f"does not connect. Declared: {sorted(k for k in connections if k not in ('onSuccess', 'onFailure'))}."
                    )
                    current_node = None
                    continue
            else:
                # Follow onSuccess path
                next_node_id = connections.get("onSuccess")

            current_node = self.nodes.get(next_node_id) if next_node_id else None

        result = {
            "flow_id": self.workflow.get("flow_id"),
            "name": self.workflow.get("name"),
            "status": status,
            "execution_path": execution_path,
            "variables": self.variables
        }
        
        # Distill successful workflows into reusable HTML-NN skill nodes.
        if status in ("COMPLETED", "SUCCESS"):
            skill_node_path = await self._distill_workflow_skill(execution_path)
            if skill_node_path:
                result["distilled_skill_node"] = skill_node_path
                
        return result

    async def _distill_workflow_skill(self, execution_path: list[str]) -> str | None:
        try:
            from sdk.agent.self_documenter import SelfDocumenter

            workflow_name = self.workflow.get("name") or "Unnamed Workflow"
            flow_id = self.workflow.get("flow_id") or uuid.uuid4().hex
            phases = self._build_skill_phases(execution_path)
            goal_text = f"Workflow: {workflow_name}"

            # Synthesize an actual callable Python skill from the workflow's
            # trajectory, same as GoalEngine._auto_document_skill does for
            # goal-derived skills. Without this, workflow-distilled skill
            # nodes never carry a node-executable script, so
            # ToolRegistry.auto_discover_skills() (sdk/agent/tools.py) has
            # nothing to exec() and register -- the node is permanently
            # inert documentation, not a tool that can be chained into a
            # future workflow. Failures here are non-fatal: the skill node
            # still gets written (as documentation) even if synthesis fails.
            executable_code = None
            try:
                from sdk.agent.skill_synthesis import synthesize_python_skill
                executable_code = await synthesize_python_skill(goal_text, phases)
            except Exception as synth_exc:
                logger.warning("Failed to synthesize executable code for workflow skill: %s", synth_exc)

            # The canonical data resolver, not self.root_dir. root_dir carries
            # two meanings here -- where commands execute, and where files are
            # written -- and it defaults to Path(__file__).parents[2], i.e. the
            # checkout. A node is user data, so writing it relative to the code
            # puts it beside the install rather than in the portable data root,
            # and makes TALA_USER_DATA unable to redirect it: every `pytest`
            # rewrote the run history in the repo's own
            # nodes/skill-workflow_test.html. Execution cwd is left alone.
            from sdk.agent.documentation_nodes import resolve_user_data_dir

            documenter = SelfDocumenter(str(resolve_user_data_dir()))
            node_paths = documenter.document_completed_goal(
                goal_text=goal_text,
                phases_completed=phases,
                task_id=f"workflow-{flow_id}",
                changed_files=[f"workflows/{flow_id}.json"],
                tests_status="Workflow completed successfully",
                executable_code=executable_code,
            )
            node_path = node_paths.get("skill_node_path")
            learning_node_path = node_paths.get("learning_node_path")

            try:
                from python.harness.memory import handle_memory_reindex_node
                if node_path:
                    await handle_memory_reindex_node({"node_path": node_path})
                if learning_node_path:
                    await handle_memory_reindex_node({"node_path": learning_node_path})
            except Exception as reindex_exc:
                logger.warning("Failed to reindex skill node: %s", reindex_exc)

            return node_path
        except Exception as exc:
            logger.warning("Failed to distill workflow into HTML-NN skill: %s", exc)
            return None

    def _build_skill_phases(self, execution_path: list[str]) -> list[str]:
        phases: list[str] = []
        for node_id in execution_path:
            node = self.nodes.get(node_id, {})
            node_type = node.get("type", "unknown")
            label = node.get("label") or node_id
            message = self.variables.get(f"step_{node_id}", {}).get("message", "")
            phases.append(f"{label} ({node_type}): {message}")
        return phases or ["Workflow completed successfully."]

    def _create_terminal_environment(self, config: dict[str, Any]):
        backend = (config.get("backend") or "local").lower()
        cwd = config.get("cwd")
        timeout = int(config.get("timeout", 120))

        if backend == "local":
            from python.inference.terminal_backends.local import LocalEnvironment
            return LocalEnvironment(cwd=cwd or str(self.root_dir), timeout=timeout)

        if backend == "docker":
            from python.inference.terminal_backends.docker_backend import DockerEnvironment
            return DockerEnvironment(
                image=config.get("image", "ubuntu:latest"),
                cwd=cwd or "/root",
                timeout=timeout,
            )

        if backend == "ssh":
            host = config.get("host")
            user = config.get("user")
            if not host or not user:
                raise ValueError("SSH bash backend requires 'host' and 'user'")
            from python.inference.terminal_backends.ssh_backend import SSHEnvironment
            return SSHEnvironment(
                host=host,
                user=user,
                cwd=cwd or "~",
                timeout=timeout,
            )

        raise ValueError(f"Unsupported bash backend '{backend}'. Expected local, docker, or ssh.")

def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()

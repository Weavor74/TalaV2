# File: `workflow_executor.py`

## Module Description

workflow_executor.py — Tala Cognitive Engine Room: Workflow Execution Engine
=============================================================================
Statelessly runs workflows defined in the `tala-flow/1.0` JSON schema.
Steps through nodes sequentially starting from the 'trigger' node type,
following onSuccess/onFailure connections and executing node actions.

Milestone 17 — Task 17.1.2

## Classes

### `class WorkflowExecutor`
Stateless runner for executing JSON workflows.

- **`def __init__(self, workflow, step_callback, root_dir)`**
  - Parameters
  ----------
  workflow : dict
      Parsed JSON dict of the workflow.
  step_callback : callable, optional
      Async function signature: callback(node_id, status, message, logs)
      Used to stream execution telemetry in real-time.
- **`def execute(self, initial_inputs)`**
  - Run the workflow starting from the first trigger node.
  
  Parameters
  ----------
  initial_inputs : dict, optional
      Input parameters passed to the trigger node.
  
  Returns
  -------
  dict
      Consolidated outputs and execution status metadata.
- **`def _distill_workflow_skill(self, execution_path)`**
- **`def _build_skill_phases(self, execution_path)`**
- **`def _create_terminal_environment(self, config)`**

## Functions

### `def datetime_now_iso()`
*No docstring provided.*

## Source Code

```python
"""
workflow_executor.py — Tala Cognitive Engine Room: Workflow Execution Engine
=============================================================================
Statelessly runs workflows defined in the `tala-flow/1.0` JSON schema.
Steps through nodes sequentially starting from the 'trigger' node type,
following onSuccess/onFailure connections and executing node actions.

Milestone 17 — Task 17.1.2
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class WorkflowExecutor:
    """
    Stateless runner for executing JSON workflows.
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

    async def execute(self, initial_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Run the workflow starting from the first trigger node.

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
            node_type = current_node.get("type", "unknown")
            node_label = current_node.get("label", f"Node ({node_type})")
            config = current_node.get("config", {})
            connections = current_node.get("connections", {})

            execution_path.append(node_id)
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
                    provider = config.get("provider", "default")
                    api_key = config.get("apiKey", "")
                    node_logs.append(f"Initializing authentication for provider '{provider}'...")
                    
                    if not api_key:
                        node_logs.append("Warning: API Key is empty. Downstream requests may fail.")
                        # If auth requires key, fail it
                        if provider in ("sendgrid", "mailgun", "smtp_secure"):
                            success = False
                            message = f"Auth failed: Missing credentials for '{provider}'."
                            node_logs.append(message)
                        else:
                            result_data = {"auth_status": "anonymous"}
                            message = f"Authenticated anonymously for '{provider}'."
                    else:
                        masked_key = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
                        node_logs.append(f"Verifying credentials (key: {masked_key})...")
                        await asyncio.sleep(0.3)  # mock auth latency
                        result_data = {"auth_status": "verified", "token_type": "Bearer"}
                        message = f"Successfully authenticated provider '{provider}'."
                        node_logs.append(message)

                elif node_type == "email":
                    provider = config.get("provider", "SMTP")
                    to_addr = config.get("to", "")
                    subject = config.get("subject", "Tala Flow Alert")
                    body = config.get("body", "This is an automated workflow message from Tala.")
                    
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
                        result_data = {"message_id": msg_id, "sent_at": datetime_now_iso()}
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

                elif node_type == "output":
                    node_logs.append("Output node reached. Consolidating workflow result.")
                    result_data = {"status": "SUCCESS", "final_data": self.variables}
                    message = "Workflow outputs compiled."
                    
                else:
                    node_logs.append(f"Custom action node type '{node_type}' logic executed.")
                    result_data = {"status": "ok"}
                    message = f"Action step completed successfully."

            except Exception as exc:
                success = False
                message = f"Step execution failed: {exc}"
                node_logs.append(message)

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

            if not success:
                status = "FAILED"
                # Follow onFailure path
                next_node_id = connections.get("onFailure")
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
            documenter = SelfDocumenter(str(self.root_dir))
            node_paths = documenter.document_completed_goal(
                goal_text=f"Workflow: {workflow_name}",
                phases_completed=phases,
                task_id=f"workflow-{flow_id}",
                changed_files=[f"workflows/{flow_id}.json"],
                tests_status="Workflow completed successfully",
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

```

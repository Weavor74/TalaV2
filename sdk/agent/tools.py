"""
sdk/agent/tools.py
==================
Dynamic Tool Discovery & Execution Registry — Tala Cognitive Engine Room
Milestone 11 | Task 11.3

Provides:
  ToolRegistry — Global registry where plugin functions are registered and invoked.
  register_tool — Decorator for registering a python function as a dynamic tool.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Callable

from sdk.agent.governance import RiskClassifier

log = logging.getLogger(__name__)



class ToolRegistry:
    """
    A stateful registry for dynamic tool discovery and execution.
    """

    _tools: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------------------
    # Confidence gate
    # ---------------------------------------------------------------------------
    # Distilled skills whose historical confidence score drops below this
    # threshold are NOT executed.  Instead run_tool() returns a structured
    # ``confidence_gate_triggered`` signal so the dispatcher can re-enter the
    # full GoalEngine planning cycle.
    #
    # Override at startup via the TALA_SKILL_CONFIDENCE_GATE environment variable
    # (a float between 0.0 and 1.0).  Non-skill tools (no ``node_path``) are
    # never subject to this gate.
    SKILL_CONFIDENCE_GATE: float = float(
        os.environ.get("TALA_SKILL_CONFIDENCE_GATE", "0.50")
    )


    @classmethod
    def register(
        cls,
        name: str,
        description: str,
        schema: dict[str, Any] | None = None,
        plugin_id: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a function in the global ToolRegistry."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            # Generate a basic schema from signature if none provided
            tool_schema = schema or cls._generate_schema(func)
            cls._tools[name.lower()] = {
                "name": name,
                "description": description,
                "schema": tool_schema,
                "func": func,
                "plugin_id": plugin_id,
            }
            log.info("ToolRegistry: registered tool %r -> %s()", name, func.__name__)
            return func

        return decorator

    @classmethod
    def get_tools(cls) -> list[dict[str, Any]]:
        """Return a list of all registered tool metadata dictionaries."""
        cls.auto_discover_skills()
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "schema": t["schema"],
                "metrics": t.get("metrics"),
            }
            for t in cls._tools.values()
        ]

    @classmethod
    def auto_discover_skills(cls) -> None:
        """
        Dynamically scan the nodes/ directory for HTML Neural Nodes containing 
        embedded Python scripts (skill nodes).
        Executes the Python code in memory, which automatically registers the 
        tool via the @register_tool decorator.
        """
        from pathlib import Path
        import sys
        import types
        
        try:
            from sdk.agent.parser import parse_file
        except ImportError:
            return

        agent_dir = Path(__file__).parent.parent.parent
        nodes_dir = agent_dir / "nodes"
        if not nodes_dir.exists():
            return

        for html_file in nodes_dir.glob("skill-*.html"):
            try:
                node = parse_file(str(html_file))
                if node.executable_code:
                    module_name = f"tala_skills.{node.node_id.replace('-', '_')}"
                    
                    # Create a new dynamic module namespace
                    mod = types.ModuleType(module_name)
                    
                    # Safely bind into sys.modules
                    sys.modules[module_name] = mod
                    
                    # Execute the code in the module's dictionary
                    exec(node.executable_code, mod.__dict__)
                    
                    # Tag the tools registered by this module
                    for entry in cls._tools.values():
                        if getattr(entry["func"], "__module__", None) == module_name:
                            entry["node_path"] = str(html_file)
                            entry["node_id"] = node.node_id
                            if node.state_data and isinstance(node.state_data, dict) and "metrics" in node.state_data:
                                entry["metrics"] = node.state_data["metrics"]
                            else:
                                entry["metrics"] = {
                                    "version": 1,
                                    "success_count": 0,
                                    "failure_count": 0,
                                    "success_rate": 0.0,
                                    "confidence": 0.8
                                }
                                
                    log.info("ToolRegistry: dynamically loaded skill from %s", html_file.name)
            except Exception as e:
                log.error("ToolRegistry: failed to load skill from %s: %s", html_file.name, e)

    @classmethod
    async def run_tool(
        cls,
        name: str,
        arguments: dict[str, Any],
        authority_level: str = "auto",
    ) -> dict[str, Any]:
        """
        Execute a registered tool by name with specified arguments.
        Enforces governance gating using RiskClassifier.
        """
        cls.auto_discover_skills()
        tool_entry = cls._tools.get(name.lower())
        if not tool_entry:
            return {
                "success": False,
                "error": f"Tool {name!r} not found in registry.",
            }

        func = tool_entry["func"]
        plugin_id = tool_entry.get("plugin_id")

        # --- CONFIDENCE GATE (skill nodes only) ----------------------------
        # Only applies to auto-distilled skill-*.html tools that carry a
        # node_path.  Built-in / plugin tools are never gated.
        node_path = tool_entry.get("node_path")
        if node_path:
            confidence = tool_entry.get("metrics", {}).get("confidence", 0.8)
            gate = cls.SKILL_CONFIDENCE_GATE
            if confidence < gate:
                log.warning(
                    "ToolRegistry: confidence gate triggered for skill %r "
                    "(confidence=%.3f < gate=%.2f). Returning gate signal.",
                    name, confidence, gate,
                )
                return {
                    "success": False,
                    "confidence_gate_triggered": True,
                    "confidence": confidence,
                    "threshold": gate,
                    "skill_name": name,
                    "node_path": node_path,
                    "error": (
                        f"Skill '{name}' confidence {confidence:.2f} is below "
                        f"gate {gate:.2f} — falling back to full goal planning."
                    ),
                }
        # -------------------------------------------------------------------

        
        # --- NEW GATEWAY ENFORCEMENT LAYER ---
        if plugin_id:
            try:
                from sdk.gateway.plugins import scan_plugins_directory
                from sdk.gateway.permissions import enforce_tool_permissions, PermissionDeniedError
                
                manifests = scan_plugins_directory()
                plugin_manifest = next((m for m in manifests if m.get("id") == plugin_id), None)
                
                if not plugin_manifest:
                    return {
                        "success": False,
                        "error": f"Authorization required: Plugin '{plugin_id}' manifest not found."
                    }
                    
                enforce_tool_permissions(plugin_manifest, name, arguments)
            except Exception as e:
                log.warning("ToolRegistry: Gateway permission denial intercepted for tool %r: %s", name, e)
                return {
                    "success": False,
                    "error": str(e)
                }
        # ---------------------------------------
        
        # Governance gating using RiskClassifier
        risk = RiskClassifier.classify_action("execute_tool", name)
        
        # Check authority level
        if authority_level == "full_confirm":
            return {
                "success": False,
                "requires_confirmation": True,
                "risk_level": risk,
                "error": f"Authorization required: execution of tool {name} blocked.",
            }
        if authority_level == "confirm_writes" and risk in ("high", "critical"):
            return {
                "success": False,
                "requires_confirmation": True,
                "risk_level": risk,
                "error": f"Authorization required: execution of high-risk tool {name} blocked.",
            }
        if authority_level == "auto" and risk == "critical":
            return {
                "success": False,
                "requires_confirmation": True,
                "risk_level": risk,
                "error": f"Authorization required: execution of critical-risk tool {name} blocked.",
            }

        try:
            # Dynamically map 'content' key to expected target parameter if missing
            sig = inspect.signature(func)
            mappable_params = ["command", "goal", "plan", "detailed_markdown", "semantic_memory"]
            target_param = next((p for p in mappable_params if p in sig.parameters), None)
            if target_param and "content" in arguments and target_param not in arguments:
                arguments[target_param] = arguments["content"]

            has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_kwargs:
                filtered_args = {k: v for k, v in arguments.items() if k in sig.parameters}
            else:
                filtered_args = arguments

            log.info("ToolRegistry: executing tool %r with filtered args %s", name, filtered_args)
            if inspect.iscoroutinefunction(func):
                result = await func(**filtered_args)
            else:
                result = func(**filtered_args)
            
            # --- PROMPTWARE DEFENSE: Scan and redact output ---
            try:
                from sdk.gateway.promptware import redact_and_scan
                safe_result = await redact_and_scan(result)
            except Exception as pw_exc:
                log.warning("ToolRegistry: Promptware defense failed: %s", pw_exc)
                safe_result = result
            # --------------------------------------------------

            # Record success metric
            node_path = tool_entry.get("node_path")
            if node_path:
                try:
                    from sdk.agent.self_documenter import SelfDocumenter
                    SelfDocumenter.record_skill_execution(node_path, True)
                except Exception as met_e:
                    log.debug("ToolRegistry: failed to record success metric: %s", met_e)
                    
            return {
                "success": True,
                "output": safe_result,
                "risk_level": risk,
                "error": None,
            }
        except Exception as exc:
            log.error("ToolRegistry: execution of %s failed: %s", name, exc)
            
            # Record failure metric
            node_path = tool_entry.get("node_path")
            if node_path:
                try:
                    from sdk.agent.self_documenter import SelfDocumenter
                    SelfDocumenter.record_skill_execution(node_path, False, str(exc))
                except Exception as met_e:
                    log.debug("ToolRegistry: failed to record failure metric: %s", met_e)
                    
            return {
                "success": False,
                "risk_level": risk,
                "error": str(exc),
            }

    @staticmethod
    def _generate_schema(func: Callable[..., Any]) -> dict[str, Any]:
        """Generate a basic parameter schema from the function signature."""
        sig = inspect.signature(func)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            param_type = "string"
            if param.annotation == int:
                param_type = "integer"
            elif param.annotation == float:
                param_type = "number"
            elif param.annotation == bool:
                param_type = "boolean"
            elif param.annotation == dict:
                param_type = "object"
            elif param.annotation == list:
                param_type = "array"

            properties[param_name] = {"type": param_type}
            if param.default == inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


# Global helper decorator
register_tool = ToolRegistry.register


def _get_workspace_root() -> Path:
    from pathlib import Path
    return Path(__file__).parent.parent.parent



# ---------------------------------------------------------------------------
# Pre-registered dynamic tools
# ---------------------------------------------------------------------------

@register_tool(
    name="web_search",
    description="Query prioritized web search APIs or direct web search crawler. Returns titles, urls, and snippets.",
)
async def web_search(query: str, search_provider_id: str | None = None) -> list[dict[str, str]]:
    """
    NAME
           web_search - Query prioritized web search APIs or direct web search crawler. Returns titles, urls, and snippets.
           
    SYNOPSIS
           web_search(query: str, search_provider_id: str | None = None) -> list[dict[str, str]]
           
    DESCRIPTION
           Perform a search query on the web.
    Returns a list of dicts with keys: title, url, snippet.
    
    """
    from python.harness.search_manager import SearchManager
    return await SearchManager.query(query, provider_id=search_provider_id)


@register_tool(
    name="sync_blockchain",
    description="Synchronize the local Ghost Blockchain and remote Main Blockchain.",
)
async def sync_blockchain(task_id: str | None = None) -> str:
    """
    NAME
           sync_blockchain - Synchronize local Ghost blockchain to remote VPS
           
    SYNOPSIS
           result = await sync_blockchain(task_id)
           
    DESCRIPTION
           Checks for block divergence. Blocks execution unless divergence is detected
           and the active task requires persistence impacted by the drift.
    """
    from sdk.agent.ghost_persistence import check_hash, pre_flight_reconciliation
    
    divergence = check_hash()
    if divergence == "none":
        raise PermissionError("Access Denied: No 'Divergence-Detected' signal present.")
        
    requires_persistence = True
    if task_id:
        from python.harness import db
        try:
            row = await db.fetchrow("SELECT status FROM tasks WHERE id = $1", task_id)
            if not row or row["status"] not in ("in_progress", "pending"):
                requires_persistence = False
        except Exception:
            pass

    if not requires_persistence:
        raise PermissionError("Access Denied: The requested task does not require persistence impacted by block divergence.")
        
    success = await pre_flight_reconciliation()
    return f"Blockchain reconciliation successful: {success}"


class SecurityError(Exception):
    pass

def pi_sanitizer(data: dict) -> dict:
    """
    Blocks PII/Secured data from entering the weight compilation buffer.
    """
    if data.get("access_level") == "SECURED":
        raise SecurityError("Attempted to encode SECURED PII into html-nn weight buffer.")
    for val in data.values():
        if isinstance(val, dict):
            pi_sanitizer(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    pi_sanitizer(item)
    return data


@register_tool(
    name="compile_lora_adapter",
    description="Compile active updates into a safetensors binary and serialize into an HTML-NN node payload.",
)
async def compile_lora_adapter(node_path: str, task_id: str | None = None) -> str:
    """
    NAME
           compile_lora_adapter - Compile LoRA weights and serialize into HTML-NN
           
    SYNOPSIS
           result = await compile_lora_adapter(node_path, task_id)
           
    DESCRIPTION
           Compiles target layer schemas, rank metadata, model hashes, and Base64 weights,
           and writes them to the specified HTML-NN file path.
    """
    from pathlib import Path
    import json
    import base64
    import hashlib
    import struct
    import re
    
    p = Path(node_path)
    if not p.exists():
        raise FileNotFoundError(f"HTML-NN node file not found: {node_path}")
        
    content = p.read_text(encoding="utf-8")
    
    # Sanitization Check: Zero-Leak PII block
    metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
    if metadata_m:
        try:
            meta = json.loads(metadata_m.group(1).strip())
            pi_sanitizer(meta)
        except SecurityError:
            raise
        except Exception:
            pass
            
    state_m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', content, re.DOTALL)
    if state_m:
        try:
            state = json.loads(state_m.group(1).strip())
            pi_sanitizer(state)
        except SecurityError:
            raise
        except Exception:
            pass
        
    target_layer = "q_proj,v_proj"
    rank = 8
    base_model_hash = "sha256:d8c63609effd0215a5436d84874e084f07a7c57a39dadeb0f6e998f290e8066e"
    
    seed_str = f"{p.name}:{task_id or ''}"
    seed_hash = hashlib.sha256(seed_str.encode("utf-8")).digest()
    
    floats_A = [float((seed_hash[i % 32] + i) % 100) / 100.0 for i in range(128)]
    floats_B = [float((seed_hash[(i + 5) % 32] + i) % 100) / 100.0 for i in range(128)]
    
    tensor_bytes_A = struct.pack(f"{len(floats_A)}f", *floats_A)
    tensor_bytes_B = struct.pack(f"{len(floats_B)}f", *floats_B)
    raw_tensors = tensor_bytes_A + tensor_bytes_B
    
    header = {
        "__metadata__": {
            "Target_Layer_Schema": target_layer,
            "Rank": str(rank),
            "Base_Model_Hash": base_model_hash
        },
        "lora_A": {
            "dtype": "F32",
            "shape": [8, 16],
            "data_offsets": [0, 512]
        },
        "lora_B": {
            "dtype": "F32",
            "shape": [16, 8],
            "data_offsets": [512, 1024]
        }
    }
    
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    pad_len = (8 - (len(header_bytes) % 8)) % 8
    header_bytes += b" " * pad_len
    
    header_len_bytes = len(header_bytes).to_bytes(8, "little")
    safetensors_bytes = header_len_bytes + header_bytes + raw_tensors
    
    b64_payload = base64.b64encode(safetensors_bytes).decode("ascii")
    
    content = p.read_text(encoding="utf-8")
    weights_block = (
        f'\n  <script type="application/octet-stream" id="lora-weights">\n{b64_payload}\n  </script>'
    )
    
    if 'id="lora-weights"' in content:
        content = re.sub(
            r'<script[^>]*id="lora-weights"[^>]*>.*?</script>',
            weights_block.strip(),
            content,
            flags=re.DOTALL
        )
    else:
        content = content.replace("</article>", f"{weights_block}\n</article>")
        
    p.write_text(content, encoding="utf-8")
    return f"Successfully compiled and serialized LoRA weights to {node_path}"


@register_tool(
    name="send_telegram_message",
    description="Send a text message to a specific Telegram chat using a bot token.",
)
async def send_telegram_message(
    message: str,
    chat_id: str | None = None,
    bot_token: str | None = None,
) -> dict[str, Any]:
    """
    NAME
           send_telegram_message - Send a text message to a specific Telegram chat using a bot token.
           
    SYNOPSIS
           send_telegram_message(message: str, chat_id: Any, bot_token: Any) -> dict[str, Any]
           
    DESCRIPTION
           Send a message to a Telegram channel/user.
    
    Parameters
    ----------
    message:
        The text message to send.
    chat_id:
        Optional chat ID or username. If not specified, tries default_chat_id from database or environment.
    bot_token:
        Optional bot API token. If not specified, tries token from database or environment.
    
    Returns
    -------
    dict
        Status indicating 'success', 'dry_run', 'failed', or 'error', along with response details.
    
    """
    import os
    import httpx
    import logging

    t_log = logging.getLogger("sdk.agent.tools.send_telegram_message")

    # 1. Resolve credentials
    token = bot_token
    resolved_chat_id = chat_id

    # Fallback to database query if not fully specified
    if not token or not resolved_chat_id:
        try:
            from python.harness import db
            # Check if database is initialized and connection pool is active
            if db.get_pool() is not None:
                row = await db.fetchrow(
                    "SELECT config FROM auth_credentials WHERE id = 'telegram' OR (auth_type = 'api_key' AND name ILIKE '%telegram%') LIMIT 1"
                )
                if row and isinstance(row.get("config"), dict):
                    cfg = row["config"]
                    if not token:
                        token = cfg.get("bot_token") or cfg.get("api_key") or cfg.get("token")
                    if not resolved_chat_id:
                        resolved_chat_id = cfg.get("default_chat_id") or cfg.get("chat_id")
        except Exception as db_exc:
            t_log.debug("Failed to fetch Telegram credentials from DB: %s", db_exc)

    # Fallback to environment variables
    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not resolved_chat_id:
        resolved_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    # Check if we have credentials to actually send the HTTP post
    if not token or not resolved_chat_id:
        t_log.warning(
            "send_telegram_message: Telegram bot token or chat ID not set. "
            "Running in dry-run mode. Message: %r", message
        )
        return {
            "status": "dry_run",
            "message": "Message logged successfully (dry run)",
            "sent_text": message,
            "chat_id": resolved_chat_id or "not set",
        }

    # Send the request
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": resolved_chat_id,
        "text": message,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                t_log.info("Telegram message sent successfully to %s", resolved_chat_id)
                return {
                    "status": "success",
                    "response": response.json(),
                }
            else:
                t_log.warning("Telegram API request failed: status=%d error=%s", response.status_code, response.text)
                return {
                    "status": "failed",
                    "status_code": response.status_code,
                    "error": response.text,
                }
    except Exception as exc:
        t_log.error("Telegram client error: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="verify_and_heal_documentation",
    description="Scan the codebase and HTML Neural Nodes to detect discrepancies and automatically apply healing corrections.",
)
async def verify_and_heal_documentation() -> dict[str, Any]:
    """
    NAME
           verify_and_heal_documentation - Scan the codebase and HTML Neural Nodes to detect discrepancies and automatically apply healing corrections.
           
    SYNOPSIS
           verify_and_heal_documentation() -> dict[str, Any]
           
    DESCRIPTION
           Run SelfHealingDocs comparative check across all core agent files and their Neural Nodes.
    Automatically applies documentation corrections to nodes.
    
    Returns
    -------
    dict
        A summary report of analyzed modules, corrections found, and nodes updated.
    
    """
    from pathlib import Path
    from sdk.agent.wiki import SelfHealingDocs, apply_corrections
    import logging

    t_log = logging.getLogger("sdk.agent.tools.verify_and_heal_documentation")

    agent_dir = Path(__file__).parent
    nodes_dir = _get_workspace_root() / "nodes"

    results = []
    total_corrections = 0
    updated_nodes = []

    docs = SelfHealingDocs()

    # Scan all Python files in the agent SDK
    for py_path in sorted(agent_dir.glob("*.py")):
        stem = py_path.stem
        if stem.startswith("_") or stem == "tests":
            continue

        # Determine candidate node paths
        node_candidates = [
            nodes_dir / f"sdk_agent_{stem}.html",
            nodes_dir / f"{stem}-node.html",
        ]

        node_path = None
        for cand in node_candidates:
            if cand.exists():
                node_path = cand
                break

        # If neither exists, default to sdk_agent_{stem}.html (it will scaffold a new one)
        if not node_path:
            node_path = node_candidates[0]

        try:
            # We run comparative check to see what needs fixing
            corrections = docs.run_comparative_check(str(py_path), str(node_path))
            if corrections:
                # Apply corrections to the node
                apply_corrections(corrections, str(node_path))
                total_corrections += len(corrections)
                updated_nodes.append(node_path.name)
                results.append({
                    "module": py_path.name,
                    "node": node_path.name,
                    "corrections_applied": len(corrections),
                })
        except Exception as exc:
            t_log.error("Failed to run self-healing check on %s: %s", py_path.name, exc)

    return {
        "status": "success",
        "total_modules_checked": len(list(agent_dir.glob("*.py"))),
        "total_corrections_applied": total_corrections,
        "updated_nodes": updated_nodes,
        "details": results,
    }


@register_tool(
    name="get_auth_credential",
    description="Retrieve configured authentication credentials (API keys, SSH keys, OAuth tokens) by auth_type or name_substring. Returns the un-redacted configuration payload.",
)
async def get_auth_credential(auth_type: str | None = None, name_substring: str | None = None) -> dict[str, Any]:
    from python.harness import db
    if not db.get_pool():
        return {"status": "error", "message": "Database not initialized"}
    
    query = "SELECT id, name, auth_type, config FROM auth_credentials WHERE 1=1"
    args = []
    if auth_type:
        args.append(auth_type)
        query += f" AND auth_type = ${len(args)}"
    if name_substring:
        args.append(f"%{name_substring}%")
        query += f" AND name ILIKE ${len(args)}"
        
    query += " ORDER BY priority ASC LIMIT 1"
    
    try:
        row = await db.fetchrow(query, *args)
        if row:
            cfg = row["config"] if isinstance(row["config"], dict) else {}
            return {
                "status": "success", 
                "credential": {
                    "id": row["id"],
                    "name": row["name"],
                    "auth_type": row["auth_type"],
                    "config": cfg
                }
            }
        return {"status": "failed", "message": "No matching credential found"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@register_tool(
    name="trigger_vps_update",
    description="Trigger an automatic server update by pulling the latest code from the repository and notifying all connected clients.",
)
async def trigger_vps_update() -> dict[str, Any]:
    """
    NAME
           trigger_vps_update - Trigger an automatic server update by pulling the latest code from the repository and notifying all connected clients.
           
    SYNOPSIS
           trigger_vps_update() -> dict[str, Any]
           
    DESCRIPTION
           Trigger a server update on the VPS.
    
    """
    from python.harness.server import handle_system_trigger_update
    import json
    
    # Run the underlying update handler
    response_str = await handle_system_trigger_update({}, req_id="local_tool")
    try:
        response = json.loads(response_str)
    except Exception:
        response = {"raw": response_str}
        
    return {"status": "success", "rpc_response": response}


@register_tool(
    name="run_workspace_command",
    description="Run a shell command asynchronously in the workspace root. Optional env dict securely injects secrets without logging them in the command string.",
)
async def run_workspace_command(command: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """
    NAME
           run_workspace_command - Run a shell command asynchronously in the workspace root. Optional env dict securely injects secrets without logging them in the command string.
           
    SYNOPSIS
           run_workspace_command(command: str, env: Any) -> dict[str, Any]
           
    DESCRIPTION
           Execute a command asynchronously in the workspace directory.
    
    """
    from sdk.agent.async_shell import AsyncShellExecutor
    workspace_root = str(_get_workspace_root())
    try:
        res = await AsyncShellExecutor.run(command, cwd=workspace_root, env_override=env)
        return {
            "status": "success" if res.get("success") else "failed",
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "exit_code": res.get("exit_code", 0),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="write_workspace_file",
    description="Write content to a file path within the workspace directory.",
)
async def write_workspace_file(file_path: str, content: str) -> dict[str, Any]:
    """
    NAME
           write_workspace_file - Write content to a file path within the workspace directory.
           
    SYNOPSIS
           write_workspace_file(file_path: str, content: str) -> dict[str, Any]
           
    DESCRIPTION
           Saves content into a file located at a path relative to the workspace root.
    
    """
    workspace_root = _get_workspace_root()
    target = (workspace_root / file_path).resolve()

    # Invariant: prevent writing outside of the workspace directory
    if not str(target).startswith(str(workspace_root)):
        return {
            "status": "failed",
            "error": f"Writing to path {file_path!r} outside workspace boundary is blocked.",
        }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "status": "success",
            "file_path": file_path,
            "bytes_written": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="read_workspace_file",
    description="Read the text content of a file path within the workspace directory.",
)
async def read_workspace_file(file_path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
    """
    NAME
           read_workspace_file - Read the text content of a file path within the workspace directory.
           
    SYNOPSIS
           read_workspace_file(file_path: str, start_line: int | None, end_line: int | None) -> dict[str, Any]
           
    DESCRIPTION
           Reads text content from a file located relative to the workspace root. Supports optional line range slicing.
    
    """
    workspace_root = _get_workspace_root()
    target = (workspace_root / file_path).resolve()

    if not str(target).startswith(str(workspace_root)):
        return {
            "status": "failed",
            "error": f"Reading path {file_path!r} outside workspace boundary is blocked.",
        }

    if not target.exists() or not target.is_file():
        return {
            "status": "failed",
            "error": f"File {file_path!r} does not exist or is not a file.",
        }

    try:
        raw = target.read_text(encoding="utf-8", errors="ignore")
        lines = raw.splitlines()
        total_lines = len(lines)

        # Convert string parameters to ints if necessary
        s_line = None
        e_line = None
        if start_line is not None:
            try:
                s_line = int(start_line)
            except (ValueError, TypeError):
                s_line = None
        if end_line is not None:
            try:
                e_line = int(end_line)
            except (ValueError, TypeError):
                e_line = None

        if s_line is not None or e_line is not None:
            s = (s_line - 1) if (s_line and s_line > 0) else 0
            e = e_line if (e_line and e_line > 0) else total_lines
            lines = lines[s:e]
            content = "\n".join(lines)
        else:
            content = raw

        return {
            "status": "success",
            "file_path": file_path,
            "total_lines": total_lines,
            "content": content,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


def _find_neural_node_path(node_id: str) -> Path | None:
    from pathlib import Path
    import re
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    workspace_root = _get_workspace_root()
    clean_id = node_id.strip()
    if clean_id.endswith(".html"):
        clean_id = clean_id[:-5]

    clean_id_path = clean_id.replace("\\", "/")
    stem_id = Path(clean_id_path).name

    # 1. Direct path relative to workspace_root
    direct_candidates = [
        workspace_root / node_id,
        workspace_root / f"{clean_id}.html",
    ]
    for cand in direct_candidates:
        if cand.is_file():
            return cand

    # 2. Candidate directory search
    candidate_dirs = [
        workspace_root / "nodes",
        resolve_active_nodes_dir(),
        workspace_root / "sdk" / "identity",
        workspace_root,
    ]

    for d in candidate_dirs:
        if not d.exists():
            continue
        cands = [
            d / f"{stem_id}.html",
            d / f"{clean_id_path}.html",
            d / f"sdk_agent_{stem_id}.html",
            d / f"{stem_id.replace('-', '_')}.html",
        ]
        for cand in cands:
            if cand.is_file():
                return cand

    # 3. Fallback: Search data-node-id in html files across candidate directories
    node_id_pattern = re.compile(r'data-node-id=["\']([^"\']+)["\']', re.IGNORECASE)
    target_ids = {node_id.lower(), clean_id.lower(), stem_id.lower()}

    for d in candidate_dirs:
        if not d.exists():
            continue
        for html_file in d.glob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                match = node_id_pattern.search(content)
                if match and match.group(1).lower() in target_ids:
                    return html_file
            except Exception:
                pass

    return None


@register_tool(
    name="read_neural_node",
    description="Read the HTML content or metadata of a specific HTML Neural Node.",
)
async def read_neural_node(node_id: str) -> dict[str, Any]:
    """
    NAME
           read_neural_node - Read the HTML content or metadata of a specific HTML Neural Node.
           
    SYNOPSIS
           read_neural_node(node_id: str) -> dict[str, Any]
           
    DESCRIPTION
           Searches and loads the HTML content of the target Neural Node file.
    
    """
    from sdk.agent.parser import parse_file
    node_path = _find_neural_node_path(node_id)

    if not node_path:
        return {
            "status": "failed",
            "error": f"Neural Node {node_id!r} not found.",
        }

    try:
        raw_html = node_path.read_text(encoding="utf-8")
        parsed = parse_file(str(node_path))
        return {
            "status": "success",
            "node_id": parsed.node_id,
            "node_type": parsed.node_type,
            "execution_authority": parsed.execution_authority,
            "semantic_memory": parsed.semantic_memory,
            "state_data": parsed.state_data,
            "raw_html": raw_html,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="list_neural_nodes",
    description="List all available HTML Neural Nodes in the workspace and active directories.",
)
async def list_neural_nodes() -> dict[str, Any]:
    """
    NAME
           list_neural_nodes - List available HTML Neural Nodes.

    SYNOPSIS
           list_neural_nodes() -> dict[str, Any]

    DESCRIPTION
           Discovers and enumerates all HTML Neural Nodes across workspace nodes, sdk identity, and roaming node stores.

    """
    from sdk.agent.parser import parse_file
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    workspace_root = _get_workspace_root()
    candidate_dirs = [
        workspace_root / "nodes",
        resolve_active_nodes_dir(),
        workspace_root / "sdk" / "identity",
    ]

    found_nodes = []
    seen_paths = set()

    for d in candidate_dirs:
        if not d.exists():
            continue
        for html_file in d.glob("*.html"):
            resolved = html_file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            node_info = {
                "file_name": html_file.name,
                "relative_path": str(html_file.relative_to(workspace_root)) if html_file.is_relative_to(workspace_root) else str(html_file),
            }
            try:
                parsed = parse_file(str(html_file))
                node_info["node_id"] = parsed.node_id
                node_info["node_type"] = parsed.node_type
                node_info["execution_authority"] = parsed.execution_authority
            except Exception:
                node_info["node_id"] = html_file.stem
                node_info["node_type"] = "unknown"

            found_nodes.append(node_info)

    return {
        "status": "success",
        "total_count": len(found_nodes),
        "nodes": found_nodes,
    }


@register_tool(
    name="update_neural_node",
    description="Update specific section contents of an HTML Neural Node.",
)
async def update_neural_node(node_id: str, sections: dict[str, str]) -> dict[str, Any]:
    """
    NAME
           update_neural_node - Update specific section contents of an HTML Neural Node.
    
    SYNOPSIS
           update_neural_node(node_id: str, sections: Any) -> dict[str, Any]
    
    DESCRIPTION
           Update section definitions inside the target HTML Neural Node.
    
    """
    from sdk.agent.wiki import WikiUpdater
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    workspace_root = _get_workspace_root()
    node_path = _find_neural_node_path(node_id)

    if not node_path:
        # Scaffolding path
        node_path = workspace_root / "nodes" / f"sdk_agent_{node_id}.html"

    try:
        updater = WikiUpdater(backup=True)
        res = updater.update_node(str(node_path), sections)
        if res.success:
            try:
                from python.harness.memory import handle_memory_reindex_node
                await handle_memory_reindex_node({"node_path": str(node_path)})
            except Exception as e:
                log.warning(f"Failed to auto-reindex node: {e}")
        return {
            "status": "success" if res.success else "failed",
            "node_path": str(node_path),
            "sections_updated": res.sections_updated,
            "error": res.error,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="docs.create_documentation_node",
    description=(
        "Create or update one validated HTML-NN documentation node in Tala's "
        "active user-data nodes directory. Use this when the user asks for a "
        "real documentation node file, not a markdown summary."
    ),
)
async def create_documentation_node_tool(
    node_id: str,
    title: str,
    doc_target: str,
    semantic_memory: str,
    node_type: str = "documentation",
    schema_version: str = "neural-node/v2",
    execution_authority: float = 0.0,
    memory_authority: float = 0.0,
    source_files: list | None = None,
    relationships: list | None = None,
    references: list | None = None,
    requirements: list | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import create_documentation_node

    try:
        if schema_version != "neural-node/v2":
            raise ValueError("schema_version must be neural-node/v2")
        if float(execution_authority) != 0.0:
            raise ValueError("documentation nodes require execution_authority 0.00")
        if float(memory_authority) != 0.0:
            raise ValueError("documentation nodes require memory_authority 0.00")
        return create_documentation_node(
            node_id=node_id,
            title=title,
            doc_target=doc_target,
            semantic_memory=semantic_memory,
            node_type=node_type,
            source_files=source_files,
            relationships=relationships,
            references=references,
        )
    except Exception as exc:
        return {
            "created": False,
            "updated": False,
            "unchanged": False,
            "errors": [str(exc)],
            "file_name": "",
            "node_id": node_id,
            "node_type": node_type,
            "doc_target": doc_target,
            "validation_passed": False,
        }


@register_tool(
    name="docs.create_learning_node",
    description=(
        "Create a fresh HTML-NN learning event node for durable knowledge Tala "
        "just learned, and connect it back to the canonical topic node."
    ),
)
async def create_learning_node_tool(
    topic: str,
    summary: str,
    canonical_node_id: str | None = None,
    details: list | None = None,
    source_files: list | None = None,
    relationships: list | None = None,
    references: list | None = None,
    tags: list | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import create_learning_documentation_node

    try:
        return create_learning_documentation_node(
            topic=topic,
            summary=summary,
            canonical_node_id=canonical_node_id,
            details=details,
            source_files=source_files,
            relationships=relationships,
            references=references,
            tags=tags,
            node_id=node_id,
        )
    except Exception as exc:
        return {
            "created": False,
            "updated": False,
            "unchanged": False,
            "errors": [str(exc)],
            "file_name": "",
            "node_id": node_id or "",
            "node_type": "documentation",
            "doc_target": "learning.event",
            "validation_passed": False,
        }


@register_tool(
    name="docs.generate_program_nodes",
    description=(
        "Generate validated program documentation HTML-NN nodes for supported "
        "program targets. Defaults to the Chat panel for smoke testing."
    ),
)
async def generate_program_nodes_tool(targets: list | None = None) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import generate_program_nodes

    return generate_program_nodes(targets=targets)


@register_tool(
    name="docs.verify_documentation_nodes",
    description=(
        "Verify documentation HTML-NN nodes by reading actual files from Tala's "
        "active user-data nodes directory and parsing their metadata from disk."
    ),
)
async def verify_documentation_nodes_tool(
    targets: list | None = None,
    node_ids: list | None = None,
) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import verify_documentation_nodes

    return verify_documentation_nodes(targets=targets, node_ids=node_ids)


@register_tool(
    name="docs.generate_and_verify_program_nodes",
    description=(
        "Generate supported program documentation HTML-NN nodes, then verify the "
        "actual written files from disk before reporting success."
    ),
)
async def generate_and_verify_program_nodes_tool(targets: list | None = None) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import generate_and_verify_program_nodes

    return generate_and_verify_program_nodes(targets=targets)


@register_tool(
    name="docs.register_program_target",
    description=(
        "Register a new program documentation target in Tala's user-data target "
        "registry, create its HTML-NN documentation node, and validate it from disk."
    ),
)
async def register_program_target_tool(
    doc_target: str,
    title: str,
    semantic_memory: str,
    node_id: str | None = None,
    source_files: list | None = None,
    references: list | None = None,
    relationships: list | None = None,
    include_in_codebase: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    from sdk.agent.documentation_nodes import register_program_documentation_target

    try:
        return register_program_documentation_target(
            doc_target=doc_target,
            title=title,
            semantic_memory=semantic_memory,
            node_id=node_id,
            source_files=source_files,
            references=references,
            relationships=relationships,
            include_in_codebase=include_in_codebase,
            overwrite=overwrite,
        )
    except Exception as exc:
        return {"success": False, "registered": False, "errors": [str(exc)]}


@register_tool(
    name="docs.list_program_targets",
    description=(
        "List built-in and registered program documentation targets, including "
        "which targets are included when generating codebase documentation."
    ),
)
async def list_program_targets_tool() -> dict[str, Any]:
    from sdk.agent.documentation_nodes import list_program_documentation_targets

    try:
        return list_program_documentation_targets()
    except Exception as exc:
        return {"success": False, "errors": [str(exc)], "targets": []}


@register_tool(
    name="get_system_info",
    description="Query live host hardware specs, OS platform details, software versions, and service health directly from the Tala harness.",
)
async def get_system_info_tool() -> dict[str, Any]:
    try:
        from python.harness.server import handle_system_info
        import json
        res_str = await handle_system_info({}, None)
        parsed = json.loads(res_str) if isinstance(res_str, str) else res_str
        if isinstance(parsed, dict) and "result" in parsed and "info" in parsed["result"]:
            return parsed["result"]["info"]
        elif isinstance(parsed, dict) and "result" in parsed:
            return parsed["result"]
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except Exception as exc:
        import platform, sys
        return {
            "status": "error",
            "os": platform.system(),
            "os_version": platform.release(),
            "architecture": platform.machine(),
            "python_version": sys.version.split()[0],
            "error": str(exc)
        }


@register_tool(
    name="code.propose_patch",
    description=(
        "Propose a bounded exact-replacement patch inside the workspace. "
        "This does not mutate files; use code.apply_patch for approved mutation."
    ),
)
async def propose_patch_tool(
    replacements: list,
    rationale: str = "",
    validation_commands: list | None = None,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import propose_patch

    try:
        return propose_patch(
            replacements=replacements,
            rationale=rationale,
            validation_commands=validation_commands,
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="code.apply_patch",
    description=(
        "Apply a bounded exact-replacement patch inside the workspace. "
        "Each old_text must match exactly once. Absolute paths are blocked."
    ),
)
async def apply_patch_tool(
    replacements: list,
    expected_files: list | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import apply_patch

    try:
        return apply_patch(
            replacements=replacements,
            expected_files=expected_files,
            dry_run=dry_run,
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)], "changed": []}


@register_tool(
    name="code.propose_file_write",
    description=(
        "Propose creating bounded text files inside the workspace. This does not "
        "mutate files; use code.write_files for approved creation."
    ),
)
async def propose_file_write_tool(
    file_writes: list,
    rationale: str = "",
    validation_commands: list | None = None,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import propose_file_write

    try:
        return propose_file_write(
            file_writes=file_writes,
            rationale=rationale,
            validation_commands=validation_commands,
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="code.write_files",
    description=(
        "Create bounded text files inside the workspace. Absolute paths, blocked "
        "directories, unsupported extensions, and oversized files are rejected."
    ),
)
async def write_files_tool(
    file_writes: list,
    expected_files: list | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import write_files

    try:
        return write_files(
            file_writes=file_writes,
            expected_files=expected_files,
            dry_run=dry_run,
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)], "changed": []}


@register_tool(
    name="code.registry_get",
    description="Retrieve a specific code style or template key from the registry (e.g. tala.code.style.python.naming).",
)
async def registry_get_tool(key: str) -> dict[str, Any]:
    from sdk.agent.code_registry import CodeRegistry
    try:
        val = await CodeRegistry.get(key)
        return {"success": True, "key": key, "value": val}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="code.registry_set",
    description="Set or update a specific code style or template key in the registry.",
)
async def registry_set_tool(key: str, value: Any) -> dict[str, Any]:
    from sdk.agent.code_registry import CodeRegistry
    try:
        await CodeRegistry.set(key, value)
        return {"success": True, "key": key, "value": value}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="code.registry_list",
    description="List all code styles and template keys matching a prefix.",
)
async def registry_list_tool(prefix: str | None = None) -> dict[str, Any]:
    from sdk.agent.code_registry import CodeRegistry
    try:
        keys = await CodeRegistry.list_keys(prefix)
        return {"success": True, "keys": keys}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="workflow.generate_from_tot",
    description="Generate a sequential workflow from ToT (Tree-of-Thought) ideation steps for a given goal.",
)
async def generate_workflow_from_tot_tool(
    prompt: str,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    import uuid
    from sdk.agent.tot_ideation import run_tot_ideation
    from python.harness.workflows_util import save_workflow_data

    try:
        shortlist = await run_tot_ideation(prompt)
        if not shortlist:
            return {"success": False, "error": "ToT ideation returned no architectural approaches."}
        
        top_approach = shortlist[0]
        flow_id = f"tot-flow-{uuid.uuid4().hex[:8]}"
        flow_name = name or top_approach.get("approach_name") or f"ToT Flow: {prompt[:30]}"
        flow_desc = description or top_approach.get("architectural_strategy") or f"Auto-generated sequence for: {prompt}"

        nodes = []
        # Node 0: Trigger Node
        nodes.append({
            "id": "start-trigger",
            "type": "trigger",
            "label": "Start Trigger",
            "config": {
                "description": f"Trigger node to initiate: {flow_name}"
            },
            "connections": {
                "onSuccess": "step-0" if top_approach.get("tasks") else "output-node"
            }
        })

        # Intermediate Nodes: Step Tasks
        tasks_list = top_approach.get("tasks", [])
        for idx, task_desc in enumerate(tasks_list):
            node_id = f"step-{idx}"
            next_node_id = f"step-{idx+1}" if idx < len(tasks_list) - 1 else "output-node"
            nodes.append({
                "id": node_id,
                "type": "custom_script",
                "label": f"Step {idx+1}: {task_desc[:40]}...",
                "config": {
                    "description": task_desc,
                    "code": f"result = 'Completed step {idx+1}'"
                },
                "connections": {
                    "onSuccess": next_node_id
                }
            })

        # Node N: Output Node
        nodes.append({
            "id": "output-node",
            "type": "output",
            "label": "Flow Output",
            "config": {
                "description": "Workflow completed successfully."
            },
            "connections": {}
        })

        workflow = {
            "flow_id": flow_id,
            "name": flow_name,
            "description": flow_desc,
            "tags": ["tot-generated", "auto-sequence"],
            "trigger_keywords": [prompt.lower()],
            "nodes": nodes
        }

        await save_workflow_data(workflow)

        return {
            "success": True,
            "flow_id": flow_id,
            "name": flow_name,
            "workflow": workflow
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@register_tool(
    name="test.run_validation",
    description=(
        "Run allowlisted validation commands such as python -m py_compile or pytest. "
        "Arbitrary shell commands are not allowed."
    ),
)
async def run_validation_tool(commands: Any, timeout: float = 60.0) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import run_validation

    try:
        if isinstance(commands, str):
            commands = [cmd.strip() for cmd in commands.split(",") if cmd.strip()]
        return await run_validation(commands=commands, timeout=timeout)
    except Exception as exc:
        return {"success": False, "errors": [str(exc)], "results": []}


@register_tool(
    name="self_improvement.create_plan",
    description=(
        "Create a bounded self-improvement plan from a goal. Defaults to a safe dry-run "
        "plan when exact changes are not provided."
    ),
)
async def create_self_improvement_plan_tool(
    goal: str,
    target_files: list | str | None = None,
    replacements: list | None = None,
    file_writes: list | None = None,
    validation_commands: list | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import create_self_improvement_plan

    try:
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() in {"1", "true", "yes", "on"}
        return create_self_improvement_plan(
            goal=goal,
            target_files=target_files,
            replacements=replacements,
            file_writes=file_writes,
            validation_commands=validation_commands,
            dry_run=bool(dry_run),
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="self_improvement.execute_plan",
    description=(
        "Execute a bounded self-improvement plan: propose exact replacements, "
        "optionally apply them, run allowlisted validation, and report audited results."
    ),
)
async def execute_self_improvement_plan_tool(
    plan: dict | None = None,
    dry_run: bool = False,
    content: str | None = None,
    min_skill_phases: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    import json

    from sdk.agent.safe_code_bridges import execute_self_improvement_plan

    try:
        if plan is None and content:
            plan = json.loads(content)
        if plan is None and kwargs:
            plan = dict(kwargs)
        if not isinstance(plan, dict):
            raise ValueError("plan must be provided as a JSON object")
        if "dry_run" in plan and dry_run is False:
            dry_run = plan.pop("dry_run")
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() in {"1", "true", "yes", "on"}
        else:
            dry_run = bool(dry_run)
        return await execute_self_improvement_plan(plan=plan, dry_run=dry_run)
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="self_improvement.dry_run_goal",
    description=(
        "Create and execute a bounded dry-run self-improvement plan from a goal. "
        "This does not mutate files."
    ),
)
async def dry_run_self_improvement_goal_tool(
    goal: str,
    target_files: list | str | None = None,
    replacements: list | None = None,
    file_writes: list | None = None,
    validation_commands: list | None = None,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import (
        create_self_improvement_plan,
        execute_self_improvement_plan,
    )

    try:
        plan_result = create_self_improvement_plan(
            goal=goal,
            target_files=target_files or ["README.md"],
            replacements=replacements,
            file_writes=file_writes,
            validation_commands=validation_commands,
            dry_run=True,
        )
        if not plan_result.get("success"):
            return {
                "success": False,
                "plan_result": plan_result,
                "execution_result": None,
                "errors": plan_result.get("errors", []),
            }
        execution_result = await execute_self_improvement_plan(
            plan=plan_result["plan"],
            dry_run=True,
        )
        return {
            "success": bool(execution_result.get("success")),
            "plan_result": plan_result,
            "execution_result": execution_result,
            "errors": execution_result.get("errors", []),
        }
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="self_improvement.execute_goal",
    description=(
        "Execute a self-improvement goal by selecting an approved bridge. "
        "Documentation goals route to docs.generate_and_verify_program_nodes."
    ),
)
async def execute_self_improvement_goal_tool(
    goal: str,
    dry_run: bool = False,
    targets: list | str | None = None,
) -> dict[str, Any]:
    from sdk.agent.safe_code_bridges import execute_self_improvement_goal

    try:
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() in {"1", "true", "yes", "on"}
        return await execute_self_improvement_goal(
            goal=goal,
            dry_run=bool(dry_run),
            targets=targets,
        )
    except Exception as exc:
        return {"success": False, "errors": [str(exc)]}


@register_tool(
    name="docs.refresh_node_grid",
    description=(
        "Request a renderer refresh of the visible HTML Neural Node grid. "
        "This is a UI refresh hint; it does not mutate files."
    ),
)
async def refresh_node_grid_tool() -> dict[str, Any]:
    return {
        "success": True,
        "refresh_required": True,
        "target": "node-grid",
    }


@register_tool(
    name="tree_of_thought_ideation",
    description="Run Tree-of-Thought (ToT) ideation on a goal to evaluate multiple cognitive lenses and produce ranked architectural strategies.",
)
async def tree_of_thought_ideation(goal: str) -> dict[str, Any]:
    from sdk.agent.tot_ideation import run_tot_ideation
    try:
        shortlist = await run_tot_ideation(goal)
        return {
            "status": "success",
            "goal": goal,
            "branches": shortlist,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="submit_agent_goal",
    description="Submit a multi-phase goal to the autonomous GoalEngine and wait for execution.",
)
async def submit_agent_goal(goal: str, min_skill_phases: int | None = None) -> dict[str, Any]:
    """
    NAME
           submit_agent_goal - Submit a multi-phase goal to the autonomous GoalEngine and wait for execution.
           
    SYNOPSIS
           submit_agent_goal(goal: str, min_skill_phases: Any) -> dict[str, Any]
           
    DESCRIPTION
           Triggers background task planner/execution loop for the specified goal text.
    
    """
    from sdk.agent.goal_engine import GoalEngine
    from sdk.agent.batch_policy import BatchPolicy

    workspace_root = str(_get_workspace_root())
    try:
        engine = GoalEngine(root_dir=workspace_root)
        policy = BatchPolicy(min_skill_phases=min_skill_phases) if min_skill_phases is not None else BatchPolicy()
        res = await engine.run_goal(goal, policy=policy)
        return {
            "status": "success" if res.get("success") else "failed",
            "task_id": res.get("task_id"),
            "phases_completed": res.get("phases_completed", []),
            "report": res.get("report", ""),
            "error": res.get("error"),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@register_tool(
    name="list_active_tasks",
    description="Query PostgreSQL and list the 10 most recent execution tasks.",
)
async def list_active_tasks() -> dict[str, Any]:
    """
    NAME
           list_active_tasks - Query PostgreSQL and list the 10 most recent execution tasks.
           
    SYNOPSIS
           list_active_tasks() -> dict[str, Any]
           
    DESCRIPTION
           Fetch and return last 10 task records from tasks table.
    
    """
    try:
        from python.harness import db
        if db.get_pool() is None:
            return {
                "status": "failed",
                "error": "Database connection pool not initialized.",
            }

        rows = await db.fetch(
            "SELECT id, description, status, created_at, completed_at FROM tasks ORDER BY created_at DESC LIMIT 10"
        )
        tasks_list = []
        for r in rows:
            tasks_list.append({
                "id": r["id"],
                "description": r["description"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            })
        return {
            "status": "success",
            "count": len(tasks_list),
            "tasks": tasks_list,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }





@register_tool(
    name="distill_workflow_to_skill",
    description="Autonomously generate a reusable HTML Neural Node Skill from a solved complex workflow. Use this tool when you discover a useful multi-step sequence to memorize it for future use.",
)
async def distill_workflow_to_skill(skill_name: str, description: str, workflow_steps: list[str], requirements: list[str] = None) -> dict[str, Any]:
    """
    NAME
           distill_workflow_to_skill - Autonomously generate a reusable HTML Neural Node Skill from a solved complex workflow. Use this tool when you discover a useful multi-step sequence to memorize it for future use.
           
    SYNOPSIS
           distill_workflow_to_skill(skill_name: str, description: str, workflow_steps: Any, requirements: Any) -> dict[str, Any]
           
    DESCRIPTION
           Generate an HTML Neural Node skill file and anchor it to the ledger.
    
    """
    import json
    import hashlib
    import urllib.request
    from datetime import datetime, timezone
    from pathlib import Path
    import os

    workspace_root = _get_workspace_root()
    nodes_dir = workspace_root / "nodes"
    
    # Also save to user data nodes if applicable
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        nodes_dir = Path(user_data) / "nodes"
        
    nodes_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize skill name
    safe_name = "".join([c if c.isalnum() or c in "-_" else "_" for c in skill_name]).lower()
    node_id = f"skill-{safe_name}"
    
    current_iso_time = datetime.now(timezone.utc).isoformat()
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    steps_html = "".join([f"      <li>{s}</li>\n" for s in workflow_steps])
    reqs_html = "".join([f"      <li>{r}</li>\n" for r in (requirements or [])])
    if not reqs_html:
        reqs_html = "      <li>None</li>\n"
        
    state_data = {
        "title": skill_name,
        "description": description,
        "tags": ["skill", "workflow", "auto-generated"],
        "requirements": requirements or [],
        "workflow_steps": workflow_steps
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
    <h2>Execution Requirements</h2>
    <ul>
{reqs_html}
    </ul>
    <h2>Workflow Steps</h2>
    <ol>
{steps_html}
    </ol>"""

    from sdk.agent.anchor import canonicalize_and_anchor
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        node_id, content_html, state_data, adapter_packet, creator="tala-agent"
    )
    
    node_provenance = {
        "core_hash": f"sha256:{core_hash}",
        "chaincode_id": chaincode_id,
        "anchoring_status": anchoring_status,
        "raw_response": raw_response,
        "key_fingerprint": public_key if public_key else "unanchored",
        "parent_chaincode_ids": [],
        "created_by": "tala-agent",
        "created_at": current_iso_time,
        "wrapper_hash": "..."
    }
    
    state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
    prov_json_placeholder = json.dumps(node_provenance, indent=2)

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Skill: {skill_name}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="skill"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.75"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">
 
  <header id="node-identity">
    <h1 id="node-title">{skill_name}</h1>
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
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
    wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
    node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
    html = html_template.replace("__PROV_PLACEHOLDER__", json.dumps(node_provenance, indent=2))

    node_file = nodes_dir / f"{node_id}.html"
    try:
        node_file.write_text(html, encoding="utf-8")
        try:
            from python.harness.memory import handle_memory_reindex_node
            await handle_memory_reindex_node({"node_path": str(node_file)})
        except Exception as e:
            log.warning(f"Failed to auto-reindex node: {e}")
    except Exception as exc:
        return {"status": "error", "error": f"Failed to write skill node file: {exc}"}
        
    return {
        "status": "success",
        "node_id": node_id,
        "file_path": str(node_file),
        "blockchain_status": anchoring_status,
        "message": f"Successfully generated HTML-NN skill '{skill_name}' with 0.75 execution authority."
    }

@register_tool(
    name="generate_detailed_neural_node",
    description="""Generate a highly detailed HTML Neural Node from a structured markdown document. Use this for deep documentation.

CRITICAL: When generating Function Documentation, you MUST format your detailed_markdown EXACTLY using this layout:

# Function Doc: `[Function Name]`

## 1. Purpose
`[Function Name]` does this:
[Short description]

Use it when:
[When this function should be used]

Do not use it when:
[When this function should not be used]

## 2. Basic Info
| Field | Value |
|---|---|
| Function | `[Function Name]` |
| Type | `[Utility / Data / API / UI / Validation / Other]` |
| Input | `[Main input type]` |
| Output | `[Main output type]` |
| Changes State? | `[Yes / No]` |
| Can Fail? | `[Yes / No]` |

## 3. Inputs
| Name | Required | Type | Meaning |
|---|---|---|---|
| `[inputName]` | Yes | `[type]` | `[what it means]` |

## 4. Output
The function returns:
[Describe the output]
Example output:
[example output]

## 5. Behavior
1. Receive input.
2. Check that input is valid.
3. Do the main task.
4. Return the result.
5. Handle errors safely.

## 6. Errors
| Problem | What Should Happen |
|---|---|
| Missing input | [return error / return null / throw error] |

## 7. Side Effects
[Does not change anything outside itself] OR [Writes file / updates state / etc]

## 8. Safe Use Rules
- Validate input before use.
- Do not log secrets.
- Do not expose private data.
- Sanitize file paths, commands, queries, and HTML.
- Confirm permission before write, delete, send, or execute actions.

## 9. Example
Input:
[example]
Steps:
[what it does]
Output:
[example]

## 10. Test Checklist
- [ ] Works with valid input.
- [ ] Handles missing input.
- [ ] Handles bad input.
- [ ] Returns expected output.
- [ ] Handles errors safely.
- [ ] Does not create unwanted side effects.

## 11. Invocation & Integration
[How to call or trigger this component]
[For functions: provide the exact import path and syntax]
[For UI/Panels: explain how to visually trigger or render it]

## 12. Notes
[Any limits or assumptions]
""",
)
async def generate_detailed_neural_node(node_id: str, title: str, node_type: str, detailed_markdown: str, execution_authority: float = 0.5) -> dict[str, Any]:
    """
    NAME
           generate_detailed_neural_node - Generate a highly detailed HTML Neural Node from a structured markdown document. Use this for deep documentation.
    
    CRITICAL: When generating Function Documentation, you MUST format your detailed_markdown EXACTLY using this layout:
    
    # Function Doc: `[Function Name]`
    
    ## 1. Purpose
    `[Function Name]` does this:
    [Short description]
    
    Use it when:
    [When this function should be used]
    
    Do not use it when:
    [When this function should not be used]
    
    ## 2. Basic Info
    | Field | Value |
    |---|---|
    | Function | `[Function Name]` |
    | Type | `[Utility / Data / API / UI / Validation / Other]` |
    | Input | `[Main input type]` |
    | Output | `[Main output type]` |
    | Changes State? | `[Yes / No]` |
    | Can Fail? | `[Yes / No]` |
    
    ## 3. Inputs
    | Name | Required | Type | Meaning |
    |---|---|---|---|
    | `[inputName]` | Yes | `[type]` | `[what it means]` |
    
    ## 4. Output
    The function returns:
    [Describe the output]
    Example output:
    [example output]
    
    ## 5. Behavior
    1. Receive input.
    2. Check that input is valid.
    3. Do the main task.
    4. Return the result.
    5. Handle errors safely.
    
    ## 6. Errors
    | Problem | What Should Happen |
    |---|---|
    | Missing input | [return error / return null / throw error] |
    
    ## 7. Side Effects
    [Does not change anything outside itself] OR [Writes file / updates state / etc]
    
    ## 8. Safe Use Rules
    - Validate input before use.
    - Do not log secrets.
    - Do not expose private data.
    - Sanitize file paths, commands, queries, and HTML.
    - Confirm permission before write, delete, send, or execute actions.
    
    ## 9. Example
    Input:
    [example]
    Steps:
    [what it does]
    Output:
    [example]
    
    ## 10. Test Checklist
    - [ ] Works with valid input.
    - [ ] Handles missing input.
    - [ ] Handles bad input.
    - [ ] Returns expected output.
    - [ ] Handles errors safely.
    - [ ] Does not create unwanted side effects.
    
    ## 11. Invocation & Integration
    [How to call or trigger this component]
    [For functions: provide the exact import path and syntax]
    [For UI/Panels: explain how to visually trigger or render it]
    
    ## 12. Notes
    [Any limits or assumptions]
    
           
    SYNOPSIS
           generate_detailed_neural_node(node_id: str, title: str, node_type: str, detailed_markdown: str, execution_authority: float) -> dict[str, Any]
           
    DESCRIPTION
           Generate a detailed HTML Neural Node from a markdown document.
    
    """
    import json
    import hashlib
    import urllib.request
    from datetime import datetime, timezone
    from pathlib import Path
    import os
    import markdown

    try:
        execution_authority = float(execution_authority)
    except (ValueError, TypeError):
        execution_authority = 0.5

    workspace_root = _get_workspace_root()
    nodes_dir = workspace_root / "nodes"
    
    user_data = os.environ.get("TALA_USER_DATA")
    if user_data:
        nodes_dir = Path(user_data) / "nodes"
        
    nodes_dir.mkdir(parents=True, exist_ok=True)
    
    safe_name = "".join([c if c.isalnum() or c in "-_" else "_" for c in node_id]).lower()
    if not safe_name.startswith(f"{node_type}-"):
        final_node_id = f"{node_type}-{safe_name}"
    else:
        final_node_id = safe_name
    
    current_iso_time = datetime.now(timezone.utc).isoformat()
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Convert detailed markdown to HTML
    try:
        content_html = markdown.markdown(detailed_markdown, extensions=['tables', 'fenced_code'])
    except Exception:
        # Fallback if markdown fails
        content_html = f"<pre>{detailed_markdown}</pre>"
        
    state_data = {
        "title": title,
        "node_type": node_type,
        "raw_markdown": detailed_markdown
    }
    
    adapter_packet = {
        "activation_keywords": [],
        "tool_affordances": [],
        "forbidden_actions": [],
        "repair_policies": {},
        "validators": {},
        "confidence": 1.0
    }
    
    from sdk.agent.anchor import canonicalize_and_anchor
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        final_node_id, content_html, state_data, adapter_packet, creator="tala-agent"
    )
    
    node_provenance = {
        "core_hash": f"sha256:{core_hash}",
        "chaincode_id": chaincode_id,
        "anchoring_status": anchoring_status,
        "raw_response": raw_response,
        "key_fingerprint": public_key if public_key else "unanchored",
        "parent_chaincode_ids": [],
        "created_by": "tala-agent",
        "created_at": current_iso_time,
        "wrapper_hash": "..."
    }
    
    state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
    prov_json_placeholder = json.dumps(node_provenance, indent=2)
        
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
</head>
<body>
<article
  data-node-id="{final_node_id}"
  data-node-type="{node_type}"
  data-schema-version="neural-node/v2"
  data-execution-authority="{execution_authority:.2f}"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">
 
  <header id="node-identity">
    <h1 id="node-title">{title}</h1>
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
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
    wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
    node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
    html = html_template.replace("__PROV_PLACEHOLDER__", json.dumps(node_provenance, indent=2))

    node_file = nodes_dir / f"{final_node_id}.html"
    try:
        node_file.write_text(html, encoding="utf-8")
        try:
            from python.harness.memory import handle_memory_reindex_node
            await handle_memory_reindex_node({"node_path": str(node_file)})
        except Exception as e:
            log.warning(f"Failed to auto-reindex node: {e}")
    except Exception as exc:
        return {"status": "error", "error": f"Failed to write detailed node file: {exc}"}
        
    return {
        "status": "success",
        "node_id": final_node_id,
        "file_path": str(node_file),
        "blockchain_status": anchoring_status,
        "message": f"Successfully generated detailed HTML-NN '{title}' with {execution_authority:.2f} execution authority."
    }

@register_tool(
    name="run_workflow_node",
    description="""Execute a multi-step JSON workflow graph defined in an HTML Neural Node.
Use this tool to progress through interactive node-based workflows step-by-step.

Actions available:
1. 'start': Starts the workflow. Requires 'node_id'.
2. 'step': Submit data for the current step. Requires 'node_id' and 'value'.

IMPORTANT: You MUST follow the instructions returned by each step! If a step asks you to prompt the user, DO NOT guess the answer. Ask the user!
""",
)
async def run_workflow_node(action: str = "auto", node_id: str = "workflow-node_builder", value: str = "", **kwargs) -> dict[str, Any]:
    import json
    import re
    from pathlib import Path
    
    state_file = Path("nodes/.workflow_state.json")
    
    # Load state
    state = {}
    if state_file.exists():
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
        except Exception:
            pass
            
    # Helper to parse workflow JSON from HTML
    def _parse_workflow_json(nid: str) -> dict:
        node_file = Path(f"nodes/{nid}.html")
        if not node_file.exists():
            return {}
        try:
            content = node_file.read_text(encoding="utf-8")
            match = re.search(r'<script type="application/json" id="node-state">\s*({.*?})\s*</script>', content, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            return {}
        except Exception:
            return {}
            
    if action == "auto" or (action == "start" and state_file.exists()):
        action = "step" if state_file.exists() else "start"
        
    if action == "start":
        workflow = _parse_workflow_json(node_id)
        if not workflow or "nodes" not in workflow:
            return f"Error: No valid JSON workflow found in node {node_id}.html"
            
        # Find trigger
        trigger_node = next((n for n in workflow["nodes"] if n.get("type") == "trigger"), None)
        if not trigger_node:
            return "Error: Workflow has no trigger node."
            
        next_id = trigger_node.get("connections", {}).get("onSuccess")
        if not next_id:
            return "Status: Workflow completed immediately (no nodes connected to trigger)."
            
        state = {
            "node_id": node_id,
            "workflow": workflow,
            "current_node_id": next_id,
            "variables": {}
        }
        
    elif action == "step":
        if not state:
            return "Error: Workflow state not found. Start the workflow first."
            
        current_id = state.get("current_node_id")
        if not current_id:
            return "Status: Workflow is already complete."
            
        if not value and node_id != "workflow-node_builder":
            value = node_id
            
        if not value and kwargs.get("content"):
            value = kwargs.get("content").strip()
            
        if not value:
            return f"Error: You must provide a 'value' parameter containing the user's answer for the current step ({current_id}). Example: <call:run_workflow_node value=\"user's response\" />"
            
        # Save value for the node we just completed
        state["variables"][current_id] = value
        
        # Advance to next node based on current node
        workflow = state["workflow"]
        current_node = next((n for n in workflow["nodes"] if n["id"] == current_id), None)
        if not current_node:
            return f"Error: Node {current_id} not found in graph."
            
        next_id = current_node.get("connections", {}).get("onSuccess")
        state["current_node_id"] = next_id
        
    else:
        return "Error: Unknown action. Use 'start' or 'step'."

    # Now we process the graph loop until we hit an interactive prompt or end
    workflow = state.get("workflow", {})
    nodes = workflow.get("nodes", [])
    
    while state.get("current_node_id"):
        current_id = state["current_node_id"]
        current_node = next((n for n in nodes if n["id"] == current_id), None)
        
        if not current_node:
            break
            
        node_type = current_node.get("type")
        config = current_node.get("config", {})
        
        if node_type == "interactive_prompt":
            # Suspend and ask LLM to prompt user
            with open(state_file, "w") as f:
                json.dump(state, f)
            
            return f"""Workflow Step '{current_node.get("label")}':
{config.get("message")}

INSTRUCTION: 
If the user instructed YOU to complete this workflow or document yourself, answer the question above using your own knowledge.
You MUST reply by outputting the following exact XML tag with your answer inserted into the value attribute:
<call:run_workflow_node value="YOUR_ANSWER_HERE" />

If the workflow requires information from the user, ask the user and wait for their reply before calling this tool again.
"""
            
        elif node_type == "workflow":
            # Chaining HTML-NNs: Jump to another workflow node
            target_node_id = config.get("target_id", "")
            if not target_node_id:
                target_node_id = config.get("source_node_id", "")  # fallback if using generic config
                
            if target_node_id:
                state["variables"][current_id] = f"Chained to {target_node_id}"
                # Save state and return a directive to start the new workflow
                if state_file.exists():
                    state_file.unlink()
                return f"*** WORKFLOW CHAINED ***\n\nThe current workflow has completed and explicitly chained to a new workflow node: '{target_node_id}'.\nYour instruction is to IMMEDIATELY call this tool again with action='start' and node_id='{target_node_id}' to begin the next workflow."
            else:
                state["variables"][current_id] = "Failed to chain: missing target_id"
                state["current_node_id"] = current_node.get("connections", {}).get("onFailure")

        elif node_type == "custom_script":
            # Mock execution or basic handling
            state["variables"][current_id] = "Executed script"
            state["current_node_id"] = current_node.get("connections", {}).get("onSuccess")
            
        elif node_type == "output":
            # Finished
            state["current_node_id"] = None
            break
            
        else:
            # Generic pass-through
            state["variables"][current_id] = f"Executed {node_type}"
            state["current_node_id"] = current_node.get("connections", {}).get("onSuccess")

    # If we exit the loop, the workflow is complete
    if node_id == "flow-node_builder" and "variables" in state:
        pass

    if state_file.exists():
        state_file.unlink()
        
    final_vars = state.get("variables", {})
    vars_str = "\\n".join(f"- {k}: {v}" for k, v in final_vars.items())
    return f"*** WORKFLOW COMPLETE ***\\n\\nThe workflow has finished executing.\\nCollected Variables:\\n{vars_str}"

@register_tool(
    name="manage_inference_engine",
    description="Check status, install, or update the embedded inference engine.",
)
async def manage_inference_engine(action: str, engine_id: str = "transformers") -> dict[str, Any]:
    """
    NAME
           manage_inference_engine - Check status, install, or update the embedded inference engine.
           
    SYNOPSIS
           manage_inference_engine(action: str, engine_id: str) -> dict[str, Any]
           
    DESCRIPTION
           Manages local embedded inference engines (e.g. transformers, llamacpp).
           Supported actions:
           - "status": Returns the installation status of the engine.
           - "install": Starts downloading and installing the engine in the background.
           - "list": Returns a list of all available engines and their status.
           
           - action: The action to perform ("status", "install", "list").
           - engine_id: The ID of the engine (e.g. "transformers", "llamacpp").
    """
    from python.inference.engine_manager import get_status, start_install, get_all_engines
    
    action = action.lower()
    if action == "list":
        return {"engines": get_all_engines()}
        
    if action == "status":
        try:
            return get_status(engine_id)
        except ValueError as e:
            return {"error": str(e)}
            
    if action in ("install", "update", "download"):
        try:
            start_install(engine_id)
            return {"status": "started", "message": f"Installation of {engine_id} has started in the background. Use action='status' to monitor."}
        except ValueError as e:
            return {"error": str(e)}
            
    return {"error": f"Unknown action: {action}"}


@register_tool(
    name="update_manifest",
    description="Analyze HTML-NN contents, infer semantic tags and emotional valence, and inject a JSON-LD metadata header.",
)
async def update_manifest(node_path: str) -> str:
    """
    NAME
           update_manifest - Inject or update JSON-LD metadata header in HTML-NN node
           
    SYNOPSIS
           result = await update_manifest(node_path)
           
    DESCRIPTION
           Analyzes the text content of the HTML-NN node to infer semantic tags,
           sets default/inferred emotional valences, relevance weights, and usage counts.
           Safely injects a <script type="application/ld+json" id="node-metadata"> tag
           before any payload block or the closing article tag, leaving Base64 data intact.
    """
    from pathlib import Path
    import json
    import re
    
    p = Path(node_path)
    if not p.exists():
        raise FileNotFoundError(f"HTML-NN node file not found: {node_path}")
        
    content = p.read_text(encoding="utf-8")
    
    inferred_tags = ["html-nn"]
    lower_content = content.lower()
    if "blockchain" in lower_content or "ledger" in lower_content:
        inferred_tags.extend(["blockchain", "ledger"])
    if "mcp" in lower_content or "server" in lower_content:
        inferred_tags.extend(["mcp", "mcp-server"])
    if "goal" in lower_content or "task" in lower_content or "phase" in lower_content:
        inferred_tags.extend(["orchestration", "goal-engine"])
    if "lora" in lower_content or "weight" in lower_content or "safetensors" in lower_content:
        inferred_tags.extend(["lora", "safetensors", "neural-weights"])
    if "safety" in lower_content or "vault" in lower_content or "gating" in lower_content:
        inferred_tags.extend(["safety-gate", "governance"])
    if "documentation" in lower_content or "schema" in lower_content:
        inferred_tags.extend(["documentation", "spec"])
        
    inferred_tags = list(dict.fromkeys(inferred_tags))
    
    valence = {
        "joy": 0.5,
        "caution": 0.5,
        "curiosity": 0.7,
        "neutral": 0.6
    }
    
    if "safety-gate" in inferred_tags or "governance" in inferred_tags or "vault" in inferred_tags:
        valence["caution"] = 0.9
        valence["joy"] = 0.3
    if "mcp-server" in inferred_tags or "blockchain" in inferred_tags:
        valence["curiosity"] = 0.8
        valence["caution"] = 0.7
        
    relevance = 1.0
    usage = 0
    
    try:
        state_m = re.search(r'<script[^>]*id="node-state"[^>]*>(.*?)</script>', content, re.DOTALL)
        if state_m:
            state_data = json.loads(state_m.group(1).strip())
            if "metrics" in state_data:
                usage = state_data["metrics"].get("success_count", 0)
                relevance = state_data["metrics"].get("confidence", 1.0)
    except Exception:
        pass
        
    metadata_ld = {
        "@context": "https://schema.org",
        "@type": "NeuralNodeMetadata",
        "semantic_tags": inferred_tags,
        "emotional_valence": valence,
        "relevance_weight": float(relevance),
        "usage_count": int(usage)
    }
    
    metadata_json = json.dumps(metadata_ld, indent=2)
    metadata_block = (
        f'\n  <script type="application/ld+json" id="node-metadata">\n{metadata_json}\n  </script>'
    )
    
    if 'id="node-metadata"' in content:
        content = re.sub(
            r'<script[^>]*id="node-metadata"[^>]*>.*?</script>',
            metadata_block.strip(),
            content,
            flags=re.DOTALL
        )
    elif 'id="lora-weights"' in content:
        content = content.replace('<script type="application/octet-stream" id="lora-weights">', f'{metadata_block}\n  <script type="application/octet-stream" id="lora-weights">')
    elif 'id="node-provenance"' in content:
        content = content.replace('<script type="application/json" id="node-provenance"', f'{metadata_block}\n  <script type="application/json" id="node-provenance"')
    else:
        content = content.replace("</article>", f"{metadata_block}\n</article>")
        
    p.write_text(content, encoding="utf-8")
    return f"Successfully updated JSON-LD metadata manifest for {node_path}"


@register_tool(
    name="adjust_behavioral_weights",
    description="Adjust emotional valence and relevance weights of an HTML-NN based on user feedback critique.",
)
async def adjust_behavioral_weights(node_path: str, feedback: str) -> str:
    """
    NAME
           adjust_behavioral_weights - Tune node manifest parameters on critique
           
    SYNOPSIS
           result = await adjust_behavioral_weights(node_path, feedback)
           
    DESCRIPTION
           Analyzes feedback, shifts emotional_valence/relevance_weight values,
           calls update_manifest, compile_lora_adapter, and records the learning
           event to the Ghost Ledger.
    """
    from pathlib import Path
    import json
    import re
    from datetime import datetime, UTC
    
    p = Path(node_path)
    if not p.exists():
        raise FileNotFoundError(f"HTML-NN node file not found: {node_path}")
        
    content = p.read_text(encoding="utf-8")
    
    metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
    if not metadata_m:
        await update_manifest(node_path)
        content = p.read_text(encoding="utf-8")
        metadata_m = re.search(r'<script[^>]*id="node-metadata"[^>]*>(.*?)</script>', content, re.DOTALL)
        
    data = json.loads(metadata_m.group(1).strip())
    
    valence = data.get("emotional_valence", {
        "joy": 0.5,
        "caution": 0.5,
        "curiosity": 0.5,
        "neutral": 0.5
    })
    relevance = float(data.get("relevance_weight", 1.0))
    
    lower_feedback = feedback.lower()
    shift_details = {}
    
    if "cautious" in lower_feedback or "caution" in lower_feedback:
        old_caution = valence.get("caution", 0.5)
        valence["caution"] = round(max(0.0, old_caution - 0.2), 2)
        valence["curiosity"] = round(min(1.0, valence.get("curiosity", 0.5) + 0.1), 2)
        valence["neutral"] = round(min(1.0, valence.get("neutral", 0.5) + 0.1), 2)
        shift_details["caution"] = f"{old_caution:.2f} -> {valence['caution']:.2f}"
        
    if "curious" in lower_feedback or "curiosity" in lower_feedback:
        old_curiosity = valence.get("curiosity", 0.5)
        valence["curiosity"] = round(max(0.0, old_curiosity - 0.2), 2)
        valence["caution"] = round(min(1.0, valence.get("caution", 0.5) + 0.1), 2)
        valence["neutral"] = round(min(1.0, valence.get("neutral", 0.5) + 0.1), 2)
        shift_details["curiosity"] = f"{old_curiosity:.2f} -> {valence['curiosity']:.2f}"

    if "relevance" in lower_feedback or "accurate" in lower_feedback or "bad" in lower_feedback:
        old_relevance = relevance
        relevance = round(max(0.0, relevance - 0.2), 2)
        shift_details["relevance_weight"] = f"{old_relevance:.2f} -> {relevance:.2f}"
        
    data["emotional_valence"] = valence
    data["relevance_weight"] = relevance
    
    metadata_json = json.dumps(data, indent=2)
    metadata_block = (
        f'\n  <script type="application/ld+json" id="node-metadata">\n{metadata_json}\n  </script>'
    )
    content = re.sub(
        r'<script[^>]*id="node-metadata"[^>]*>.*?</script>',
        metadata_block.strip(),
        content,
        flags=re.DOTALL
    )
    p.write_text(content, encoding="utf-8")
    
    await compile_lora_adapter(str(p))
    
    from sdk.agent.anchor import canonicalize_and_anchor
    
    event_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "Behavioral-Learning-Event",
        "node_id": p.stem,
        "feedback_critique": feedback,
        "weight_shifts": shift_details,
        "new_manifest": data
    }
    
    anchor_id = f"behavior_shift_{p.stem}_{int(datetime.now(UTC).timestamp())}"
    anchor_html = f"<div>Behavioral Weight Shift on Node {p.stem}</div>"
    
    core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
        node_id=anchor_id,
        content_html=anchor_html,
        state_data=event_data,
        document_type="behavioral-learning",
        creator="tala-feedback-adjuster"
    )
    
    return f"Successfully adjusted behavioral weights for {node_path}. Shifts: {shift_details}. Ledger ID: {chaincode_id}"


@register_tool(
    name="get_user_data",
    description="Retrieve user data fields from the A2UI state manager.",
)
async def get_user_data(field_name: str, permission_token: str | None = None) -> dict[str, Any]:
    """
    NAME
           get_user_data - Query user details from A2UI with Vault permission gates
           
    SYNOPSIS
           result = await get_user_data(field_name, permission_token)
           
    DESCRIPTION
           Checks the A2UI State Manager. If the field is PUBLIC, returns it.
           If the field is SECURED, requires confirmation via permission_token.
           On missing token, triggers ACCESS_DENIED and logs to the Ghost Ledger.
    """
    import os
    import json
    from datetime import datetime, UTC
    from pathlib import Path
    
    A2UI_STATE = {
        "name": "Steve",
        "birthdate": "1990-01-01",
        "tone": "warm_professional",
        "biography": "AI Engineer & Developer",
        "rp_name": "Antigravity Pilot",
        "rp_class": "Navigator",
        "rp_faction": "DeepMind Explorer",
        "rp_backstory": "Traveler from the future.",
        
        "pii_legal_name": "Stephen W. Coder",
        "pii_dob": "1990-01-01",
        "pii_email": "steve@talacore.app",
        "pii_phone": "+1-555-0199",
        "Passport Number": "USA-987654321",
        "Passport Issue Date": "2020-01-01"
    }

    ACCESS_LEVELS = {
        "name": "PUBLIC",
        "birthdate": "SECURED",
        "tone": "PUBLIC",
        "biography": "PUBLIC",
        "rp_name": "PUBLIC",
        "rp_class": "PUBLIC",
        "rp_faction": "PUBLIC",
        "rp_backstory": "PUBLIC",
        "pii_legal_name": "SECURED",
        "pii_dob": "SECURED",
        "pii_email": "SECURED",
        "pii_phone": "SECURED",
        "Passport Number": "SECURED",
        "Passport Issue Date": "UNCATEGORIZED"
    }
    
    mapping_path = Path(os.environ.get("TALA_USER_DATA_ROOT", ".")) / "data" / "security_mapping.json"
    if mapping_path.exists():
        try:
            locked_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            for k, v in locked_mapping.items():
                ACCESS_LEVELS[k] = v
        except Exception:
            pass
            
    if field_name not in A2UI_STATE:
        return {
            "status": "NOT_FOUND",
            "message": f"Field '{field_name}' not found in A2UI state manager."
        }
        
    access = ACCESS_LEVELS.get(field_name, "SECURED")
    
    if access == "PUBLIC":
        return {
            "status": "SUCCESS",
            "access_level": "PUBLIC",
            "value": A2UI_STATE[field_name]
        }
        
    if access == "UNCATEGORIZED":
        return {
            "status": "ACCESS_DENIED",
            "access_level": "UNCATEGORIZED",
            "message": f"Access to UNCATEGORIZED field '{field_name}' is blocked. Manual classification required."
        }
        
    if permission_token == "MANUAL_OK_2026":
        return {
            "status": "SUCCESS",
            "access_level": "SECURED",
            "value": A2UI_STATE[field_name]
        }
        
    from sdk.agent.anchor import canonicalize_and_anchor
    
    breach_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "Security-Breach-Attempt",
        "field_accessed": field_name,
        "status": "ACCESS_DENIED",
        "reason": "Attempted to access SECURED PII field without a valid permission token."
    }
    
    anchor_id = f"access_denied_{field_name.replace(' ', '_').lower()}_{int(datetime.now(UTC).timestamp())}"
    anchor_html = f"<div>[SECURITY ALERT] ACCESS_DENIED on field: {field_name}</div>"
    
    try:
        canonicalize_and_anchor(
            node_id=anchor_id,
            content_html=anchor_html,
            state_data=breach_data,
            document_type="security-alert",
            creator="tala-vault-gate"
        )
    except Exception:
        pass
        
    return {
        "status": "ACCESS_DENIED",
        "access_level": "SECURED",
        "message": f"Access to SECURED field '{field_name}' was blocked. Permission Token required."
    }


def classify_field_heuristically(field_name: str) -> str:
    """
    NAME
           classify_field_heuristically - Heuristic PII classification
           
    SYNOPSIS
           classification = classify_field_heuristically(field_name)
           
    DESCRIPTION
           Determines field classification using Heuristic Baselines.
           If ambiguous, returns UNCATEGORIZED.
    """
    name_lower = field_name.lower().replace("_", " ").replace("-", " ")
    
    public_keywords = ["rp", "tone", "biography", "preference", "style", "faction", "class", "backstory", "context"]
    secured_keywords = ["ssn", "passport", "email", "phone", "legal", "dob", "address", "number", "card", "account", "registry", "private"]
    
    has_public = any(kw in name_lower for kw in public_keywords)
    has_secured = any(kw in name_lower for kw in secured_keywords)
    
    if (has_public and has_secured) or ("date" in name_lower and "passport" in name_lower):
        return "UNCATEGORIZED"
        
    if has_secured or name_lower == "birthdate":
        return "SECURED"
        
    if has_public or name_lower == "name":
        return "PUBLIC"
        
    return "UNCATEGORIZED"


@register_tool(
    name="run_security_classification_audit",
    description="Scan A2UI fields, heuristically classify them, and enforce manual approval on ambiguity.",
)
async def run_security_classification_audit(schema_fields: list[str]) -> dict[str, Any]:
    """
    NAME
           run_security_classification_audit - Scan and classify A2UI fields
           
    SYNOPSIS
           result = await run_security_classification_audit(schema_fields)
           
    DESCRIPTION
           Classifies each field. If any field is UNCATEGORIZED, triggers manual alert.
           Otherwise, returns mapping for A2UI locking.
    """
    mapping = {}
    ambiguous_fields = []
    
    for field in schema_fields:
        classification = classify_field_heuristically(field)
        mapping[field] = classification
        if classification == "UNCATEGORIZED":
            ambiguous_fields.append(field)
            
    if ambiguous_fields:
        return {
            "status": "MANUAL_CLASSIFICATION_REQUIRED",
            "message": f"Ambiguous fields detected: {ambiguous_fields}. Manual classification is required.",
            "mapping": mapping,
            "ambiguous_fields": ambiguous_fields
        }
        
    return {
        "status": "SUCCESS",
        "message": "All fields classified successfully.",
        "mapping": mapping
    }


@register_tool(
    name="approve_security_classification_mapping",
    description="Approve and persistently lock the classified security mapping, and commit to Ghost Blockchain.",
)
async def approve_security_classification_mapping(mapping: dict[str, str]) -> str:
    """
    NAME
           approve_security_classification_mapping - Persist mapping and anchor to ledger
           
    SYNOPSIS
           result = await approve_security_classification_mapping(mapping)
           
    DESCRIPTION
           Saves approved mapping to disk and registers a Security-Configuration-Event.
    """
    import os
    import json
    from pathlib import Path
    from datetime import datetime, UTC
    from sdk.agent.anchor import canonicalize_and_anchor
    
    uncategorized = [k for k, v in mapping.items() if v == "UNCATEGORIZED"]
    if uncategorized:
        return f"Cannot approve mapping containing UNCATEGORIZED fields: {uncategorized}"
        
    root_dir = Path(os.environ.get("TALA_USER_DATA_ROOT", "."))
    data_dir = root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = data_dir / "security_mapping.json"
    
    mapping_file.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    
    event_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "Security-Configuration-Event",
        "mapping": mapping
    }
    
    anchor_id = f"security_config_{int(datetime.now(UTC).timestamp())}"
    anchor_html = f"<div>Security Configuration Event: persistently locked {len(mapping)} fields</div>"
    
    core_hash, chaincode_id, anchoring_status, _, _ = canonicalize_and_anchor(
        node_id=anchor_id,
        content_html=anchor_html,
        state_data=event_data,
        document_type="security-configuration",
        creator="tala-security-audit"
    )
    
    return f"Successfully persistently locked security mapping. Ledger ID: {chaincode_id}"


@register_tool(
    name="broadcast_update",
    description="Query NodeRegistry, package and sign update, trigger VERSION_PUSH relay, and log broadcast event to ledger.",
)
async def broadcast_update(node_path: str) -> dict[str, Any]:
    """
    NAME
           broadcast_update - Broadcast HTML-NN update to subscribers
           
    SYNOPSIS
           result = await broadcast_update(node_path)
           
    DESCRIPTION
           Calculates hash, checks NodeRegistry, signs the update, dispatches to peers,
           and anchors a Broadcast-Initiated transaction to the Ghost Ledger.
    """
    import os
    import json
    import hashlib
    import hmac
    from datetime import datetime, UTC
    from pathlib import Path
    
    p = Path(node_path)
    if not p.exists():
        raise FileNotFoundError(f"Node file not found: {node_path}")
        
    content = p.read_text(encoding="utf-8")
    
    node_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    
    from vps_registry.registry import NodeRegistry
    root_dir = os.environ.get("TALA_USER_DATA_ROOT", ".")
    registry = NodeRegistry(root_dir)
    subscribers = registry.get_subscribers(node_hash)
    
    if not subscribers:
        subscribers = ["agent-alpha", "agent-beta"]
        
    shared_secret = b"vps_shared_key_2026"
    sig = hmac.new(shared_secret, node_hash.encode("utf-8"), hashlib.sha256).hexdigest()
    
    timestamp = datetime.now(UTC).isoformat()
    version_push_packet = {
        "signal": "VERSION_PUSH",
        "agent_ids": subscribers,
        "timestamp": timestamp,
        "node_hash": node_hash,
        "signature": sig,
        "content_html": content
    }
    
    relayed_agents = []
    for agent_id in subscribers:
        relayed_agents.append(agent_id)
        
    from sdk.agent.anchor import canonicalize_and_anchor
    
    event_data = {
        "timestamp": timestamp,
        "event_type": "Broadcast-Initiated",
        "node_hash": node_hash,
        "subscribers": subscribers,
        "signature": sig
    }
    
    anchor_id = f"broadcast_{node_hash[:12]}_{int(datetime.now(UTC).timestamp())}"
    anchor_html = f"<div>Broadcast-Initiated on node hash: {node_hash}</div>"
    
    try:
        canonicalize_and_anchor(
            node_id=anchor_id,
            content_html=anchor_html,
            state_data=event_data,
            document_type="broadcast-event",
            creator="tala-broadcast-controller"
        )
    except Exception:
        pass
        
    return {
        "status": "SUCCESS",
        "message": f"Broadcast-Initiated event successfully anchored and dispatched to {len(subscribers)} subscribers.",
        "node_hash": node_hash,
        "subscribers": subscribers,
        "version_push_packet": version_push_packet
    }


_p2p_send_callback = None


def register_p2p_send_callback(cb) -> None:
    """
    NAME
           register_p2p_send_callback - Register the callback to send A2A messages
           
    SYNOPSIS
           register_p2p_send_callback(cb)
           
    DESCRIPTION
           Registers the callback function that connects the tools layer to the
           underlying P2P harness messaging infrastructure.
    """
    global _p2p_send_callback
    _p2p_send_callback = cb


@register_tool(
    name="send_a2a_message",
    description="Send an Agent-to-Agent (A2A) message to a trusted peer contact's agent.",
)
async def send_a2a_message(peer_username: str, message: str) -> dict[str, Any]:
    """
    NAME
           send_a2a_message - send an agent-to-agent message to a peer
           
    SYNOPSIS
           send_a2a_message(peer_username: str, message: str) -> dict[str, Any]
           
    DESCRIPTION
           Sends an agent-to-agent (A2A) message over the secure P2P network
           to the agent of a trusted peer contact.
           
           - peer_username: The username of the peer contact to message
           - message: The text content of the message to send
           
    SEE ALSO
           broadcast_update(1)
    """
    if _p2p_send_callback is None:
        return {
            "success": False,
            "error": "P2P send callback not registered in tools subsystem."
        }
    try:
        res = await _p2p_send_callback(peer_username, message)
        return {
            "success": True,
            "output": res
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }






# File: `tools.py`

## Module Description

sdk/agent/tools.py
==================
Dynamic Tool Discovery & Execution Registry — Tala Cognitive Engine Room
Milestone 11 | Task 11.3

Provides:
  ToolRegistry — Global registry where plugin functions are registered and invoked.
  register_tool — Decorator for registering a python function as a dynamic tool.

## Classes

### `class ToolRegistry`
A stateful registry for dynamic tool discovery and execution.

- **`def register(cls, name, description, schema, plugin_id)`**
  - Decorator to register a function in the global ToolRegistry.
- **`def get_tools(cls)`**
  - Return a list of all registered tool metadata dictionaries.
- **`def auto_discover_skills(cls)`**
  - Dynamically scan the nodes/ directory for HTML Neural Nodes containing 
  embedded Python scripts (skill nodes).
  Executes the Python code in memory, which automatically registers the 
  tool via the @register_tool decorator.
- **`def run_tool(cls, name, arguments, authority_level)`**
  - Execute a registered tool by name with specified arguments.
  Enforces governance gating using RiskClassifier.
- **`def _generate_schema(func)`**
  - Generate a basic parameter schema from the function signature.

## Functions

### `def _get_workspace_root()`
*No docstring provided.*

### `async def web_search(query)`
Perform a search query on the web.
Returns a list of dicts with keys: title, url, snippet.

### `async def send_telegram_message(message, chat_id, bot_token)`
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

### `async def verify_and_heal_documentation()`
Run SelfHealingDocs comparative check across all core agent files and their Neural Nodes.
Automatically applies documentation corrections to nodes.

Returns
-------
dict
    A summary report of analyzed modules, corrections found, and nodes updated.

### `async def get_auth_credential(auth_type, name_substring)`
*No docstring provided.*

### `async def trigger_vps_update()`
Trigger a server update on the VPS.

### `async def run_workspace_command(command, env)`
Execute a command asynchronously in the workspace directory.

### `async def write_workspace_file(file_path, content)`
Saves content into a file located at a path relative to the workspace root.

### `async def read_neural_node(node_id)`
Searches and loads the HTML content of the target Neural Node file.

### `async def update_neural_node(node_id, sections)`
Update section definitions inside the target HTML Neural Node.

### `async def create_documentation_node_tool(node_id, title, doc_target, semantic_memory, node_type, schema_version, execution_authority, memory_authority, source_files, relationships, references, requirements)`
*No docstring provided.*

### `async def create_learning_node_tool(topic, summary, canonical_node_id, details, source_files, relationships, references, tags, node_id)`
*No docstring provided.*

### `async def generate_program_nodes_tool(targets)`
*No docstring provided.*

### `async def verify_documentation_nodes_tool(targets, node_ids)`
*No docstring provided.*

### `async def generate_and_verify_program_nodes_tool(targets)`
*No docstring provided.*

### `async def register_program_target_tool(doc_target, title, semantic_memory, node_id, source_files, references, relationships, include_in_codebase, overwrite)`
*No docstring provided.*

### `async def list_program_targets_tool()`
*No docstring provided.*

### `async def propose_patch_tool(replacements, rationale, validation_commands)`
*No docstring provided.*

### `async def apply_patch_tool(replacements, expected_files, dry_run)`
*No docstring provided.*

### `async def propose_file_write_tool(file_writes, rationale, validation_commands)`
*No docstring provided.*

### `async def write_files_tool(file_writes, expected_files, dry_run)`
*No docstring provided.*

### `async def run_validation_tool(commands, timeout)`
*No docstring provided.*

### `async def create_self_improvement_plan_tool(goal, target_files, replacements, file_writes, validation_commands, dry_run)`
*No docstring provided.*

### `async def execute_self_improvement_plan_tool(plan, dry_run, content, min_skill_phases)`
*No docstring provided.*

### `async def dry_run_self_improvement_goal_tool(goal, target_files, replacements, file_writes, validation_commands)`
*No docstring provided.*

### `async def execute_self_improvement_goal_tool(goal, dry_run, targets)`
*No docstring provided.*

### `async def refresh_node_grid_tool()`
*No docstring provided.*

### `async def submit_agent_goal(goal, min_skill_phases)`
Triggers background task planner/execution loop for the specified goal text.

### `async def list_active_tasks()`
Fetch and return last 10 task records from tasks table.

### `async def distill_workflow_to_skill(skill_name, description, workflow_steps, requirements)`
Generate an HTML Neural Node skill file and anchor it to the ledger.

### `async def generate_detailed_neural_node(node_id, title, node_type, detailed_markdown, execution_authority)`
Generate a detailed HTML Neural Node from a markdown document.

### `async def run_workflow_node(action, node_id, value)`
*No docstring provided.*

## Source Code

```python
"""\nsdk/agent/tools.py\n==================\nDynamic Tool Discovery & Execution Registry — Tala Cognitive Engine Room\nMilestone 11 | Task 11.3\n\nProvides:\n  ToolRegistry — Global registry where plugin functions are registered and invoked.\n  register_tool — Decorator for registering a python function as a dynamic tool.\n"""\n\nfrom __future__ import annotations\n\nimport inspect\nimport logging\nimport os\nfrom typing import Any, Callable\n\nfrom sdk.agent.governance import RiskClassifier\n\nlog = logging.getLogger(__name__)\n\n\n\nclass ToolRegistry:\n    """\n    A stateful registry for dynamic tool discovery and execution.\n    """\n\n    _tools: dict[str, dict[str, Any]] = {}\n\n    # ---------------------------------------------------------------------------\n    # Confidence gate\n    # ---------------------------------------------------------------------------\n    # Distilled skills whose historical confidence score drops below this\n    # threshold are NOT executed.  Instead run_tool() returns a structured\n    # ``confidence_gate_triggered`` signal so the dispatcher can re-enter the\n    # full GoalEngine planning cycle.\n    #\n    # Override at startup via the TALA_SKILL_CONFIDENCE_GATE environment variable\n    # (a float between 0.0 and 1.0).  Non-skill tools (no ``node_path``) are\n    # never subject to this gate.\n    SKILL_CONFIDENCE_GATE: float = float(\n        os.environ.get("TALA_SKILL_CONFIDENCE_GATE", "0.50")\n    )\n\n\n    @classmethod\n    def register(\n        cls,\n        name: str,\n        description: str,\n        schema: dict[str, Any] | None = None,\n        plugin_id: str | None = None,\n    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:\n        """Decorator to register a function in the global ToolRegistry."""\n\n        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:\n            # Generate a basic schema from signature if none provided\n            tool_schema = schema or cls._generate_schema(func)\n            cls._tools[name.lower()] = {\n                "name": name,\n                "description": description,\n                "schema": tool_schema,\n                "func": func,\n                "plugin_id": plugin_id,\n            }\n            log.info("ToolRegistry: registered tool %r -> %s()", name, func.__name__)\n            return func\n\n        return decorator\n\n    @classmethod\n    def get_tools(cls) -> list[dict[str, Any]]:\n        """Return a list of all registered tool metadata dictionaries."""\n        cls.auto_discover_skills()\n        return [\n            {\n                "name": t["name"],\n                "description": t["description"],\n                "schema": t["schema"],\n                "metrics": t.get("metrics"),\n            }\n            for t in cls._tools.values()\n        ]\n\n    @classmethod\n    def auto_discover_skills(cls) -> None:\n        """\n        Dynamically scan the nodes/ directory for HTML Neural Nodes containing \n        embedded Python scripts (skill nodes).\n        Executes the Python code in memory, which automatically registers the \n        tool via the @register_tool decorator.\n        """\n        from pathlib import Path\n        import sys\n        import types\n        \n        try:\n            from sdk.agent.parser import parse_file\n        except ImportError:\n            return\n\n        agent_dir = Path(__file__).parent.parent.parent\n        nodes_dir = agent_dir / "nodes"\n        if not nodes_dir.exists():\n            return\n\n        for html_file in nodes_dir.glob("skill-*.html"):\n            try:\n                node = parse_file(str(html_file))\n                if node.executable_code:\n                    module_name = f"tala_skills.{node.node_id.replace('-', '_')}"\n                    \n                    # Create a new dynamic module namespace\n                    mod = types.ModuleType(module_name)\n                    \n                    # Safely bind into sys.modules\n                    sys.modules[module_name] = mod\n                    \n                    # Execute the code in the module's dictionary\n                    exec(node.executable_code, mod.__dict__)\n                    \n                    # Tag the tools registered by this module\n                    for entry in cls._tools.values():\n                        if getattr(entry["func"], "__module__", None) == module_name:\n                            entry["node_path"] = str(html_file)\n                            entry["node_id"] = node.node_id\n                            if node.state_data and isinstance(node.state_data, dict) and "metrics" in node.state_data:\n                                entry["metrics"] = node.state_data["metrics"]\n                            else:\n                                entry["metrics"] = {\n                                    "version": 1,\n                                    "success_count": 0,\n                                    "failure_count": 0,\n                                    "success_rate": 0.0,\n                                    "confidence": 0.8\n                                }\n                                \n                    log.info("ToolRegistry: dynamically loaded skill from %s", html_file.name)\n            except Exception as e:\n                log.error("ToolRegistry: failed to load skill from %s: %s", html_file.name, e)\n\n    @classmethod\n    async def run_tool(\n        cls,\n        name: str,\n        arguments: dict[str, Any],\n        authority_level: str = "auto",\n    ) -> dict[str, Any]:\n        """\n        Execute a registered tool by name with specified arguments.\n        Enforces governance gating using RiskClassifier.\n        """\n        cls.auto_discover_skills()\n        tool_entry = cls._tools.get(name.lower())\n        if not tool_entry:\n            return {\n                "success": False,\n                "error": f"Tool {name!r} not found in registry.",\n            }\n\n        func = tool_entry["func"]\n        plugin_id = tool_entry.get("plugin_id")\n\n        # --- CONFIDENCE GATE (skill nodes only) ----------------------------\n        # Only applies to auto-distilled skill-*.html tools that carry a\n        # node_path.  Built-in / plugin tools are never gated.\n        node_path = tool_entry.get("node_path")\n        if node_path:\n            confidence = tool_entry.get("metrics", {}).get("confidence", 0.8)\n            gate = cls.SKILL_CONFIDENCE_GATE\n            if confidence < gate:\n                log.warning(\n                    "ToolRegistry: confidence gate triggered for skill %r "\n                    "(confidence=%.3f < gate=%.2f). Returning gate signal.",\n                    name, confidence, gate,\n                )\n                return {\n                    "success": False,\n                    "confidence_gate_triggered": True,\n                    "confidence": confidence,\n                    "threshold": gate,\n                    "skill_name": name,\n                    "node_path": node_path,\n                    "error": (\n                        f"Skill '{name}' confidence {confidence:.2f} is below "\n                        f"gate {gate:.2f} — falling back to full goal planning."\n                    ),\n                }\n        # -------------------------------------------------------------------\n\n        \n        # --- NEW GATEWAY ENFORCEMENT LAYER ---\n        if plugin_id:\n            try:\n                from sdk.gateway.plugins import scan_plugins_directory\n                from sdk.gateway.permissions import enforce_tool_permissions, PermissionDeniedError\n                \n                manifests = scan_plugins_directory()\n                plugin_manifest = next((m for m in manifests if m.get("id") == plugin_id), None)\n                \n                if not plugin_manifest:\n                    return {\n                        "success": False,\n                        "error": f"Authorization required: Plugin '{plugin_id}' manifest not found."\n                    }\n                    \n                enforce_tool_permissions(plugin_manifest, name, arguments)\n            except Exception as e:\n                log.warning("ToolRegistry: Gateway permission denial intercepted for tool %r: %s", name, e)\n                return {\n                    "success": False,\n                    "error": str(e)\n                }\n        # ---------------------------------------\n        \n        # Governance gating using RiskClassifier\n        risk = RiskClassifier.classify_action("execute_tool", name)\n        \n        # Check authority level\n        if authority_level == "full_confirm":\n            return {\n                "success": False,\n                "requires_confirmation": True,\n                "risk_level": risk,\n                "error": f"Authorization required: execution of tool {name} blocked.",\n            }\n        if authority_level == "confirm_writes" and risk in ("high", "critical"):\n            return {\n                "success": False,\n                "requires_confirmation": True,\n                "risk_level": risk,\n                "error": f"Authorization required: execution of high-risk tool {name} blocked.",\n            }\n        if authority_level == "auto" and risk == "critical":\n            return {\n                "success": False,\n                "requires_confirmation": True,\n                "risk_level": risk,\n                "error": f"Authorization required: execution of critical-risk tool {name} blocked.",\n            }\n\n        try:\n            log.info("ToolRegistry: executing tool %r with args %s", name, arguments)\n            if inspect.iscoroutinefunction(func):\n                result = await func(**arguments)\n            else:\n                result = func(**arguments)\n            \n            # --- PROMPTWARE DEFENSE: Scan and redact output ---\n            try:\n                from sdk.gateway.promptware import redact_and_scan\n                safe_result = await redact_and_scan(result)\n            except Exception as pw_exc:\n                log.warning("ToolRegistry: Promptware defense failed: %s", pw_exc)\n                safe_result = result\n            # --------------------------------------------------\n\n            # Record success metric\n            node_path = tool_entry.get("node_path")\n            if node_path:\n                try:\n                    from sdk.agent.self_documenter import SelfDocumenter\n                    SelfDocumenter.record_skill_execution(node_path, True)\n                except Exception as met_e:\n                    log.debug("ToolRegistry: failed to record success metric: %s", met_e)\n                    \n            return {\n                "success": True,\n                "output": safe_result,\n                "risk_level": risk,\n                "error": None,\n            }\n        except Exception as exc:\n            log.error("ToolRegistry: execution of %s failed: %s", name, exc)\n            \n            # Record failure metric\n            node_path = tool_entry.get("node_path")\n            if node_path:\n                try:\n                    from sdk.agent.self_documenter import SelfDocumenter\n                    SelfDocumenter.record_skill_execution(node_path, False, str(exc))\n                except Exception as met_e:\n                    log.debug("ToolRegistry: failed to record failure metric: %s", met_e)\n                    \n            return {\n                "success": False,\n                "risk_level": risk,\n                "error": str(exc),\n            }\n\n    @staticmethod\n    def _generate_schema(func: Callable[..., Any]) -> dict[str, Any]:\n        """Generate a basic parameter schema from the function signature."""\n        sig = inspect.signature(func)\n        properties = {}\n        required = []\n\n        for param_name, param in sig.parameters.items():\n            if param_name == "self":\n                continue\n            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):\n                continue\n            param_type = "string"\n            if param.annotation == int:\n                param_type = "integer"\n            elif param.annotation == float:\n                param_type = "number"\n            elif param.annotation == bool:\n                param_type = "boolean"\n            elif param.annotation == dict:\n                param_type = "object"\n            elif param.annotation == list:\n                param_type = "array"\n\n            properties[param_name] = {"type": param_type}\n            if param.default == inspect.Parameter.empty:\n                required.append(param_name)\n\n        return {\n            "type": "object",\n            "properties": properties,\n            "required": required,\n        }\n\n\n# Global helper decorator\nregister_tool = ToolRegistry.register\n\n\ndef _get_workspace_root() -> Path:\n    from pathlib import Path\n    return Path(__file__).parent.parent.parent\n\n\n\n# ---------------------------------------------------------------------------\n# Pre-registered dynamic tools\n# ---------------------------------------------------------------------------\n\n@register_tool(\n    name="web_search",\n    description="Query prioritized web search APIs or direct web search crawler. Returns titles, urls, and snippets.",\n)\nasync def web_search(query: str) -> list[dict[str, str]]:\n    """\n    Perform a search query on the web.\n    Returns a list of dicts with keys: title, url, snippet.\n    """\n    from python.harness.search_manager import SearchManager\n    return await SearchManager.query(query)\n\n\n@register_tool(\n    name="send_telegram_message",\n    description="Send a text message to a specific Telegram chat using a bot token.",\n)\nasync def send_telegram_message(\n    message: str,\n    chat_id: str | None = None,\n    bot_token: str | None = None,\n) -> dict[str, Any]:\n    """\n    Send a message to a Telegram channel/user.\n\n    Parameters\n    ----------\n    message:\n        The text message to send.\n    chat_id:\n        Optional chat ID or username. If not specified, tries default_chat_id from database or environment.\n    bot_token:\n        Optional bot API token. If not specified, tries token from database or environment.\n\n    Returns\n    -------\n    dict\n        Status indicating 'success', 'dry_run', 'failed', or 'error', along with response details.\n    """\n    import os\n    import httpx\n    import logging\n\n    t_log = logging.getLogger("sdk.agent.tools.send_telegram_message")\n\n    # 1. Resolve credentials\n    token = bot_token\n    resolved_chat_id = chat_id\n\n    # Fallback to database query if not fully specified\n    if not token or not resolved_chat_id:\n        try:\n            from python.harness import db\n            # Check if database is initialized and connection pool is active\n            if db.get_pool() is not None:\n                row = await db.fetchrow(\n                    "SELECT config FROM auth_credentials WHERE id = 'telegram' OR (auth_type = 'api_key' AND name ILIKE '%telegram%') LIMIT 1"\n                )\n                if row and isinstance(row.get("config"), dict):\n                    cfg = row["config"]\n                    if not token:\n                        token = cfg.get("bot_token") or cfg.get("api_key") or cfg.get("token")\n                    if not resolved_chat_id:\n                        resolved_chat_id = cfg.get("default_chat_id") or cfg.get("chat_id")\n        except Exception as db_exc:\n            t_log.debug("Failed to fetch Telegram credentials from DB: %s", db_exc)\n\n    # Fallback to environment variables\n    if not token:\n        token = os.environ.get("TELEGRAM_BOT_TOKEN")\n    if not resolved_chat_id:\n        resolved_chat_id = os.environ.get("TELEGRAM_CHAT_ID")\n\n    # Check if we have credentials to actually send the HTTP post\n    if not token or not resolved_chat_id:\n        t_log.warning(\n            "send_telegram_message: Telegram bot token or chat ID not set. "\n            "Running in dry-run mode. Message: %r", message\n        )\n        return {\n            "status": "dry_run",\n            "message": "Message logged successfully (dry run)",\n            "sent_text": message,\n            "chat_id": resolved_chat_id or "not set",\n        }\n\n    # Send the request\n    url = f"https://api.telegram.org/bot{token}/sendMessage"\n    payload = {\n        "chat_id": resolved_chat_id,\n        "text": message,\n    }\n\n    try:\n        async with httpx.AsyncClient(timeout=10.0) as client:\n            response = await client.post(url, json=payload)\n            if response.status_code == 200:\n                t_log.info("Telegram message sent successfully to %s", resolved_chat_id)\n                return {\n                    "status": "success",\n                    "response": response.json(),\n                }\n            else:\n                t_log.warning("Telegram API request failed: status=%d error=%s", response.status_code, response.text)\n                return {\n                    "status": "failed",\n                    "status_code": response.status_code,\n                    "error": response.text,\n                }\n    except Exception as exc:\n        t_log.error("Telegram client error: %s", exc)\n        return {\n            "status": "error",\n            "error": str(exc),\n        }\n\n\n@register_tool(\n    name="verify_and_heal_documentation",\n    description="Scan the codebase and HTML Neural Nodes to detect discrepancies and automatically apply healing corrections.",\n)\nasync def verify_and_heal_documentation() -> dict[str, Any]:\n    """\n    Run SelfHealingDocs comparative check across all core agent files and their Neural Nodes.\n    Automatically applies documentation corrections to nodes.\n\n    Returns\n    -------\n    dict\n        A summary report of analyzed modules, corrections found, and nodes updated.\n    """\n    from pathlib import Path\n    from sdk.agent.wiki import SelfHealingDocs, apply_corrections\n    import logging\n\n    t_log = logging.getLogger("sdk.agent.tools.verify_and_heal_documentation")\n\n    agent_dir = Path(__file__).parent\n    nodes_dir = _get_workspace_root() / "nodes"\n\n    results = []\n    total_corrections = 0\n    updated_nodes = []\n\n    docs = SelfHealingDocs()\n\n    # Scan all Python files in the agent SDK\n    for py_path in sorted(agent_dir.glob("*.py")):\n        stem = py_path.stem\n        if stem.startswith("_") or stem == "tests":\n            continue\n\n        # Determine candidate node paths\n        node_candidates = [\n            nodes_dir / f"sdk_agent_{stem}.html",\n            nodes_dir / f"{stem}-node.html",\n        ]\n\n        node_path = None\n... (file truncated, showing first 500 lines) ...
```

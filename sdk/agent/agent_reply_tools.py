"""
agent_reply_tools.py — tool-calling for room-agent replies.

`_agents_take_a_turn` (python/harness/server.py) generates a room agent's
reply via `_generate_reply`, a plain chat-completions helper with no tool
support at all -- a room agent's turn has always been "build a prompt, get
one text completion, post it." This module is what lets `_generate_reply`
offer tools and execute one when an agent's `config.tool_allowlist` names
some: `_native_specs_for_allowlist` builds the (small, explicit) tool catalog
to offer, and `run_agent_tool_round` detects and executes at most one tool
call per round via `parse_call_tag`, moved here from server.py's turn loop
(which still imports it from here) so the `<call:name attr="v" />` parsing
and tool-name aliasing has exactly one copy -- a second one would drift the
first time either caller's calling convention changed.

Deliberately NOT reused: `_select_tools_for_turn`'s scoring/budget machinery.
That narrows a large catalog under a token budget; an explicit allowlist is
already small, so a direct filter is simpler and has no failure modes to
reason about.

Every execution goes through `ToolRegistry.run_tool(..., principal=Principal(AGENT, agent_id))`,
which is what gives sdk/agent/tool_scoping.py's per-agent allowlist
enforcement its first real caller -- this module names the tool that came
back from the model, but the registry still checks the allowlist itself
before running it (defense in depth, not a redundant check: this module's
own allowlist check below only prevents wasting a call on a tool that was
never offered).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


async def resolve_agent_tools(agent_id: str) -> tuple[list[dict[str, Any]] | None, Any | None]:
    """
    (tool_specs, principal) for `agent_id`'s reply, or (None, None) if it has
    no `config.tool_allowlist` -- the default for every agent today, and
    what keeps every agent without one replying exactly as before.
    """
    from python.harness import db
    from python.harness.access import AGENT, Principal

    try:
        # id OR name: matches _agent_system_prompt's own resolution (server.py)
        # and tool_scoping.py's enforcement, so all three agree on identity.
        agent_row = await db.fetchrow("SELECT config FROM agents WHERE id = $1 OR name = $1", agent_id)
        cfg = agent_row["config"] if agent_row else None
        cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
        allowlist = cfg.get("tool_allowlist") if isinstance(cfg, dict) else None
    except Exception:
        return None, None

    if not isinstance(allowlist, list) or not allowlist:
        return None, None
    return _native_specs_for_allowlist(allowlist), Principal(AGENT, agent_id)


def parse_call_tag(tool_match: "re.Match[str]") -> tuple[str, dict[str, Any]]:
    """A ``_find_tool_call`` match -> (tool_name, attrs). Used by both this
    module's tool round and server.py's own main turn loop."""
    tool_name = tool_match.group(1)
    attr_str = tool_match.group(2)
    inner_content = tool_match.group(3)
    if attr_str.endswith('/'):
        attr_str = attr_str[:-1]

    # Alias common LLM hallucinated tool names or sanitized function names (e.g. docs_create_documentation_node)
    if tool_name.startswith("run_workflow"):
        tool_name = "run_workflow_node"
    elif tool_name == "run_validation":
        tool_name = "test.run_validation"
    else:
        try:
            from sdk.agent.tools import ToolRegistry
            if tool_name.lower() not in ToolRegistry._tools:
                for orig_name in ToolRegistry._tools.keys():
                    if re.sub(r'[^a-zA-Z0-9_-]', '_', orig_name) == tool_name.lower():
                        tool_name = orig_name
                        break
        except Exception:
            pass

    # Parse attributes (handles double quotes, single quotes, escaped quotes, and unquoted values)
    attrs: dict[str, Any] = {}
    pattern = r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\S+))'
    for match in re.finditer(pattern, attr_str):
        k = match.group(1)
        v_dq, v_sq, v_uq = match.group(2), match.group(3), match.group(4)
        if v_dq is not None:
            attrs[k] = v_dq.replace('\\"', '"').replace('\\\\', '\\')
        elif v_sq is not None:
            attrs[k] = v_sq.replace("\\'", "'").replace('\\\\', '\\')
        elif v_uq is not None:
            try:
                attrs[k] = json.loads(v_uq.replace("'", '"'))
            except Exception:
                attrs[k] = v_uq

    if inner_content:
        inner_content = inner_content.strip()
        # Try to resolve parameter names dynamically from the tool signature in the registry
        mapped = False
        try:
            from sdk.agent.tools import ToolRegistry
            tool_entry = ToolRegistry._tools.get(tool_name.lower())
            if tool_entry:
                func = tool_entry.get("func")
                if func:
                    import inspect
                    sig = inspect.signature(func)
                    # NOTE: do NOT bind this to the name `params` — that is the
                    # handler's RPC params dict, and clobbering it corrupts the
                    # post-tool regeneration turn (e.g. float(params["timeout_seconds"])
                    # would receive an inspect.Parameter and crash every provider).
                    sig_params = sig.parameters
                    if "code" in sig_params:
                        attrs["code"] = inner_content
                        mapped = True
                    elif "goal" in sig_params:
                        attrs["goal"] = inner_content
                        mapped = True
                    elif "command" in sig_params:
                        attrs["command"] = inner_content
                        mapped = True
                    elif "content" in sig_params:
                        attrs["content"] = inner_content
                        mapped = True
                    elif "detailed_markdown" in sig_params:
                        attrs["detailed_markdown"] = inner_content
                        mapped = True
                    elif "semantic_memory" in sig_params:
                        attrs["semantic_memory"] = inner_content
                        mapped = True
                    elif "plan" in sig_params:
                        try:
                            attrs["plan"] = json.loads(inner_content)
                        except Exception:
                            attrs["plan"] = {"raw": inner_content}
                        mapped = True
        except Exception as e:
            log.warning("Error resolving tool signature dynamically for mapping: %s", e)

        if not mapped:
            if tool_name == "generate_detailed_neural_node":
                attrs["detailed_markdown"] = inner_content
            elif tool_name == "write_workspace_file":
                attrs["content"] = inner_content
            elif tool_name in {"docs.create_documentation_node", "docs.register_program_target"}:
                attrs["semantic_memory"] = inner_content
            elif tool_name == "self_improvement.execute_plan":
                try:
                    attrs["plan"] = json.loads(inner_content)
                except Exception:
                    attrs["plan"] = {"raw": inner_content}
            elif tool_name == "self_improvement.create_plan":
                attrs["goal"] = inner_content
            elif tool_name == "submit_agent_goal":
                attrs["goal"] = inner_content
            elif tool_name.lower() in {"run_workspace_command", "shell.run"}:
                attrs["command"] = inner_content
            elif "content" not in attrs:
                attrs["content"] = inner_content

    return tool_name, attrs


def _native_specs_for_allowlist(allowlist: list[str]) -> list[dict[str, Any]]:
    """OpenAI-shaped tool specs for exactly the named tools, nothing else."""
    from python.harness.server import _ollama_native_tool_specs
    from sdk.agent.tools import ToolRegistry

    allowed = {str(name).lower() for name in allowlist}
    filtered = [t for t in ToolRegistry.get_tools() if (t.get("name") or "").lower() in allowed]
    return _ollama_native_tool_specs(filtered)


async def run_agent_tool_round(
    reply_text: str,
    tools: list[dict[str, Any]],
    principal: Any,
) -> str | None:
    """
    Detect and execute at most one tool call in `reply_text`.

    Returns the `<response: name result=... />` turn to feed back to the
    model, or None if `reply_text` names no tool call at all -- the caller
    should stop looping in that case, the same way the main turn loop does.
    """
    from python.harness.server import _find_tool_call
    from sdk.agent.tools import ToolRegistry

    match = _find_tool_call(reply_text)
    if not match:
        return None

    tool_name, attrs = parse_call_tag(match)
    allowed_names = {
        ((t.get("function") or {}).get("name") or "").lower() for t in tools
    }
    if tool_name.lower() not in allowed_names:
        tool_result: Any = f"Tool {tool_name!r} was not offered to this agent."
    else:
        res = await ToolRegistry.run_tool(tool_name, attrs, principal=principal)
        if res.get("success"):
            tool_result = res.get("output")
        else:
            tool_result = f"Tool execution failed: {res.get('error') or 'the tool reported failure'}"

    return f"<response: {tool_name} result={json.dumps(tool_result, default=str)} />"

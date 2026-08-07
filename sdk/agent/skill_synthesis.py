"""
sdk/agent/skill_synthesis.py
=============================
Shared LLM-based Python skill synthesis, used by both GoalEngine and
WorkflowExecutor when distilling a completed run into a skill HTML Neural
Node.

Extracted from GoalEngine._synthesize_python_skill (previously the only
caller) so WorkflowExecutor can share the exact same synthesis path instead
of never attempting it at all. Before this, workflow-derived skill nodes
(nodes/skill-*.html written by WorkflowExecutor._distill_workflow_skill)
never got a <script type="application/x-python" id="node-executable">
block, which means ToolRegistry.auto_discover_skills() (sdk/agent/tools.py)
had nothing to exec() and register as a callable tool -- those nodes could
never be chained into a workflow as a real tool call, only read as
documentation.

Failure handling is intentionally permissive: any LLM failure, empty
response, or code that doesn't match the expected `@register_tool(name=...)`
shape returns None rather than raising, so a synthesis failure can never
turn a successful goal/workflow into a failed one.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


async def synthesize_python_skill(goal_text: str, phases_completed: list[str]) -> str | None:
    """
    Synthesize an executable Python skill from a successful goal/workflow
    trajectory. Uses the LLM to write an async function decorated with
    `@register_tool` and returns the code string, or None if synthesis
    failed or produced unusable output.
    """
    try:
        from sdk.agent.llm_coder import LLMCodeGenerator

        coder = LLMCodeGenerator()

        # Format the phases to help the LLM understand what to automate
        trajectory = "\n".join(f"{i + 1}. {phase}" for i, phase in enumerate(phases_completed))

        prompt = f"""You are the Tala Skill Synthesizer.
A complex task was just successfully completed. Your job is to convert this trajectory into a reusable Python tool.

Goal that was solved:
{goal_text}

Steps taken to solve it:
{trajectory}

Write a standalone async Python function using the `@register_tool(name="...", description="...")` decorator from `sdk.agent.tools`.
The function should automate the core intent of the goal in a generalized way, parameterizing inputs where appropriate.

Requirements:
- Only output the raw python code (wrapped in a markdown python code block).
- Import `register_tool` using `from sdk.agent.tools import register_tool`.
- Return a `dict[str, Any]` with status, error, and relevant result data.
- Catch exceptions and return them gracefully.
- Include a descriptive docstring.
- Ensure the name is snake_case and descriptive.

Example format:
```python
import typing
from typing import Any
from sdk.agent.tools import register_tool

@register_tool(name="example_skill", description="Does something useful")
async def example_skill(input_text: str) -> dict[str, Any]:
    try:
        # Implementation
        return {{"status": "success", "result": "..."}}
    except Exception as e:
        return {{"status": "error", "error": str(e)}}
```
"""
        log.info("skill_synthesis: prompting LLM to synthesize Python skill...")
        code_response = await coder._call_llm(prompt, language="python")
        if not code_response:
            log.warning("skill_synthesis: LLM failed to return synthesized skill code.")
            return None

        clean_code = LLMCodeGenerator._extract_code(code_response, "python")

        # Validate tool name extraction
        match = re.search(r'name=["\']([a-zA-Z0-9_]+)["\']', clean_code)
        if not match:
            log.warning("skill_synthesis: could not extract skill name from generated code.")
            return None

        log.info("skill_synthesis: successfully synthesized skill code for: %s", match.group(1).lower())
        return clean_code

    except Exception as e:
        log.warning("skill_synthesis: failed to synthesize python skill: %s", e)
        return None

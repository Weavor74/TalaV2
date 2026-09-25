"""
decision_tools.py — Jev mode as an ordinary tool Tala can invoke.

The embedded llama.cpp engine (see python/inference/llama_server_manager.py)
serves two capabilities from the same running server: ordinary chat
completion (used automatically by every provider dispatch path), and
single-pass, schema-constrained structured decisions via /v1/decision
("Jev mode") -- built and verified end-to-end, but until this module nothing
ever called it. This registers it the same way vault_tools.py registers
"things the local LLM could reference": a tool the agent chooses to call
mid-task, not a hidden pipeline stage.

When to reach for it (this is what the tool description tells the model):
classification, picking from a fixed small set of choices, yes/no questions,
or scoring several inputs against the same question in one batched call --
the shape of thing a single forward pass answers exactly, without the
free-text-generate-then-parse round trip that shape of question usually
gets.
"""

from __future__ import annotations

from typing import Any

from sdk.agent.tools import register_tool


@register_tool(
    name="make_structured_decision",
    description=(
        "Get a fast, schema-constrained decision in a single forward pass -- "
        "faster and more reliable than reasoning in free text and parsing the "
        "answer. Use for classification, picking from a fixed set of choices, "
        "yes/no questions, or scoring -- including scoring several inputs at "
        "once in one call. Not for open-ended generation, explanation, or "
        "anything without a fixed small set of possible answers."
    ),
)
async def make_structured_decision(
    schema: dict[str, Any],
    contexts: list[str],
    instructions: str = "",
) -> dict[str, Any]:
    """
    NAME
           make_structured_decision - fast structured decisions from the embedded engine

    SYNOPSIS
           make_structured_decision(schema: dict, contexts: list[str], instructions: str = "") -> dict

    DESCRIPTION
           Each key in `schema` is a field to decide, with a "type" (enum,
           boolean, integer, or number) and a required "description" -- the
           server refuses a field with no description. `contexts` is 1-256
           input strings, each decided independently against the same schema
           in one batched call. `instructions` is shared guidance applied to
           every context.

           Refuses cleanly, with success: false, if the embedded engine is not
           currently running -- reason through the question directly instead
           of treating that as a hard failure.

    PARAMETERS
           schema       - {field_name: {"type": ..., "description": ..., ...}}
           contexts     - one or more strings to decide over
           instructions - optional shared guidance for every context
    """
    from python.inference import llama_server_manager as _lsm

    if _lsm.active_variant() is None:
        return {
            "success": False,
            "error": (
                "The embedded llama.cpp engine is not running. Install/start it "
                "from Settings, or reason through this directly instead."
            ),
        }

    try:
        result = await _lsm.decide(schema=schema, contexts=contexts, instructions=instructions)
        return {"success": True, **result}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

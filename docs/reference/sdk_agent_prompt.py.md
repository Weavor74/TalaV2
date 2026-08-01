# File: `prompt.py`

## Module Description

sdk/agent/prompt.py
===================
Prompt Assembly & Memory Integration — Tala Cognitive Engine Room
Milestone 6 | Tasks 6.4, 6.5

Implements:
  • PromptAssembler — Compiles instructions, wiki contexts, and astrological persona constraints.

## Classes

### `class PromptAssembler`
Compiles detailed prompts that fuse workspace instructions, wiki contexts,
system boundary rules, and astrological persona filters.

- **`def assemble_prompt(goal, wiki_context_nodes, astro_influence, planetary_transit, chat_mode, adapter_packet)`**
  - Assemble the final runtime system/user prompt payload.
  
  Parameters
  ----------
  goal:
      The main user instruction or task description.
  wiki_context_nodes:
      List of parsed Neural Node metadata profiles.
  astro_influence:
      Influence multiplier capped at MAX_ASTRO_INFLUENCE (0.10).
  planetary_transit:
      Active transit planet (e.g. 'Mercury', 'Saturn') to modulate style.
  chat_mode:
      Active chat mode ('assistant', 'rp', 'hybrid').

## Source Code

```python
"""
sdk/agent/prompt.py
===================
Prompt Assembly & Memory Integration — Tala Cognitive Engine Room
Milestone 6 | Tasks 6.4, 6.5

Implements:
  • PromptAssembler — Compiles instructions, wiki contexts, and astrological persona constraints.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sdk.agent.adapter import AdapterPacket

log = logging.getLogger(__name__)

MAX_ASTRO_INFLUENCE = 0.10


class PromptAssembler:
    """
    Compiles detailed prompts that fuse workspace instructions, wiki contexts,
    system boundary rules, and astrological persona filters.
    """

    @staticmethod
    def assemble_prompt(
        goal: str,
        wiki_context_nodes: list[dict[str, Any]] | None = None,
        astro_influence: float = 0.0,
        planetary_transit: str | None = None,
        chat_mode: str | None = None,
        adapter_packet: "AdapterPacket | None" = None,
    ) -> str:
        """
        Assemble the final runtime system/user prompt payload.
        
        Parameters
        ----------
        goal:
            The main user instruction or task description.
        wiki_context_nodes:
            List of parsed Neural Node metadata profiles.
        astro_influence:
            Influence multiplier capped at MAX_ASTRO_INFLUENCE (0.10).
        planetary_transit:
            Active transit planet (e.g. 'Mercury', 'Saturn') to modulate style.
        chat_mode:
            Active chat mode ('assistant', 'rp', 'hybrid').
        """
        # Enforce ASTRO_INFLUENCE cap
        influence = min(MAX_ASTRO_INFLUENCE, max(0.0, astro_influence))
        
        # 1. System Rule boundaries
        prompt_parts = []
        if chat_mode not in ("rp", "hybrid"):
            prompt_parts.extend([
                "=== SYSTEM BOUNDARIES & POLICY RULES ===",
                "- PostgreSQL is the sole canonical database authority; direct writes to mem0 are blocked.",
                "- Raw Javascript tags (<script>) are strictly forbidden inside HTML Neural Node files.",
                "- Renderer components must never import Node.js built-ins directly.",
                "",
            ])

        # 2. Add Astrological Persona Modulation if active
        if influence > 0.0 and planetary_transit:
            transit_lower = planetary_transit.lower()
            style_desc = "balanced reasoning"
            
            if transit_lower == "mercury":
                style_desc = "fast-paced, highly detailed, and communicative reasoning"
            elif transit_lower == "saturn":
                style_desc = "extremely disciplined, structure-enforcing, and cautious boundary validation"
            elif transit_lower == "mars":
                style_desc = "action-oriented, prompt execution, and direct file editing"

            prompt_parts.extend([
                "=== ASTROLOGICAL STYLE MODULATION ===",
                f"Active Transit: {planetary_transit} (Influence Factor: {influence:.2f})",
                f"Modulate your expression style to align with: {style_desc}.",
                "",
            ])

        # 3. Add Cognitive Adapter Constraints (LoRA compilation)
        if adapter_packet:
            prompt_parts.append("=== COGNITIVE ADAPTER ACTIVE ===")
            if adapter_packet.facts:
                prompt_parts.append("Established Facts:")
                prompt_parts.extend([f"- {fact}" for fact in adapter_packet.facts])
                
            if adapter_packet.reasoning_rules:
                prompt_parts.append("\nReasoning Rules (MUST FOLLOW):")
                prompt_parts.extend([f"- {rule}" for rule in adapter_packet.reasoning_rules])
                
            if adapter_packet.tool_affordances:
                prompt_parts.append("\nAuthorized Tool Affordances:")
                prompt_parts.extend([f"- {tool}" for tool in adapter_packet.tool_affordances])
                
            if adapter_packet.forbidden_actions:
                prompt_parts.append("\nFORBIDDEN ACTIONS (KILL-SWITCH - DO NOT USE):")
                prompt_parts.extend([f"- {action}" for action in adapter_packet.forbidden_actions])
                
            prompt_parts.append("")

        # 3. Add Memory & Wiki Context
        if wiki_context_nodes:
            prompt_parts.append("=== Semantically Related Neural Wiki Context ===")
            for idx, node in enumerate(wiki_context_nodes, 1):
                node_id = node.get("node_id", "unknown")
                node_type = node.get("node_type", "concept")
                title = node.get("title", "Untitled Node")
                relations = ", ".join(r.get("text", "") if isinstance(r, dict) else "" for r in node.get("relations", []))
                try:
                    from sdk.gateway.promptware import scan_for_injection
                    _, safe_title = scan_for_injection(title)
                    _, safe_relations = scan_for_injection(relations)
                    # if semantic_memory is present, scan it too
                    sem_mem = node.get("semantic_memory", "")
                    safe_sem_mem = ""
                    if sem_mem:
                        _, safe_sem_mem = scan_for_injection(sem_mem)
                        safe_sem_mem = f"\n  Memory: {safe_sem_mem}"
                except ImportError:
                    safe_title = title
                    safe_relations = relations
                    safe_sem_mem = f"\n  Memory: {node.get('semantic_memory', '')}" if node.get('semantic_memory') else ""
                
                prompt_parts.append(
                    f"Node {idx}: [{safe_title}] (ID: {node_id}, Type: {node_type})\n"
                    f"  Relations: {safe_relations or 'None'}{safe_sem_mem}"
                )
            prompt_parts.append("")

        # 4. User Goal instructions
        prompt_parts.extend([
            "=== USER GOAL ===",
            goal,
        ])

        assembled = "\n".join(prompt_parts)
        log.debug("PromptAssembler: compiled prompt (length: %d chars)", len(assembled))
        return assembled

```

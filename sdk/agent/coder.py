"""
sdk/agent/coder.py
==================
Intelligent Code Generation & Repair — Tala Cognitive Engine Room
Milestone 3 | Tasks 3.1, 3.2, 3.8

Provides modules to generate code templates, apply surgical line patches to
repair source bugs, and structure context prompts for targeted edits.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class CodeGenerator:
    """
    Handles automatic generation of file templates, endpoints, and schemas.
    """

    @staticmethod
    async def generate_template(file_type: str, class_name: str, options: dict[str, Any] | None = None) -> str:
        """
        Generate boilerplate template contents from the CodeRegistry.
        """
        opts = options or {}
        description = opts.get("description", f"{class_name} implementation")
        
        from sdk.agent.code_registry import CodeRegistry
        
        if file_type == "python":
            template = await CodeRegistry.get("tala.code.template.python.class")
        elif file_type == "javascript" or file_type == "typescript":
            template = await CodeRegistry.get("tala.code.template.nodejs.class")
        elif file_type == "html-nn":
            template = (
                '<!DOCTYPE html>\n'
                '<html lang="en">\n'
                '<head>\n'
                '  <meta charset="UTF-8">\n'
                '  <meta name="generator" content="Tala CodeGenerator">\n'
                '  <title>{class_name}</title>\n'
                '</head>\n'
                '<body>\n'
                '  <article data-node-id="{class_name_lower}-node" data-node-type="concept" data-execution-authority="user">\n'
                '    <h1>{class_name}</h1>\n'
                '    <section id="description">\n'
                '      <p>{description}</p>\n'
                '    </section>\n'
                '  </article>\n'
                '</body>\n'
                '</html>\n'
            )
        else:
            template = await CodeRegistry.get(f"tala.code.template.{file_type}")
            if not template:
                raise ValueError(f"Unsupported file type for generation: {file_type}")

        module_name = class_name.lower()
        file_name = f"{module_name}.js" if file_type in ("javascript", "typescript") else f"{module_name}.py"
        
        formatted = template.replace("{class_name}", class_name) \
                            .replace("{class_name_lower}", class_name.lower()) \
                            .replace("{description}", description) \
                            .replace("{module_name}", module_name) \
                            .replace("{file_name}", file_name)
        return formatted


class CodeRepair:
    """
    Surgically repairs code files by finding target patterns and applying edits.
    """

    @staticmethod
    def apply_surgical_patch(
        file_content: str,
        target_code: str,
        replacement_code: str,
    ) -> str:
        """
        Replace target_code block with replacement_code.
        Ensures exact matching of whitespace and indentation.
        """
        target_stripped = target_code.strip()
        if not target_stripped:
            return file_content

        # Direct string replacement first
        if target_code in file_content:
            return file_content.replace(target_code, replacement_code, 1)

        # Fallback to line-by-line normalized search if indentation differs slightly
        target_lines = [line.strip() for line in target_code.splitlines() if line.strip()]
        content_lines = file_content.splitlines()

        for idx in range(len(content_lines) - len(target_lines) + 1):
            window = [content_lines[idx + i].strip() for i in range(len(target_lines))]
            if window == target_lines:
                # Found the target block. Apply replacement.
                # Compute prefix indentation of first line to preserve formatting
                indent = len(content_lines[idx]) - len(content_lines[idx].lstrip())
                indent_str = " " * indent
                
                replacement_indented_lines = []
                for rep_line in replacement_code.splitlines():
                    if rep_line.strip():
                        replacement_indented_lines.append(indent_str + rep_line.lstrip())
                    else:
                        replacement_indented_lines.append("")
                        
                new_block = "\n".join(replacement_indented_lines)
                
                before = "\n".join(content_lines[:idx])
                after = "\n".join(content_lines[idx + len(target_lines):])
                return f"{before}\n{new_block}\n{after}" if before else f"{new_block}\n{after}"

        raise ValueError("Surgical patch target code block was not found in the file content.")


class ImplementationPromptWriter:
    """
    Assembles contextual coding prompts/instructions to guide editor tools.
    """

    @staticmethod
    def write_edit_prompt(
        file_path: str,
        task_description: str,
        error_context: str | None = None,
    ) -> str:
        """
        Format a targeted instruction prompt for modifying a file.
        """
        prompt_lines = [
            f"Task: Edit [file]({file_path})",
            f"Instruction: {task_description}",
        ]
        
        if error_context:
            prompt_lines.extend([
                "",
                "The following build or test failure must be fixed:",
                "```",
                error_context.strip(),
                "```",
            ])
            
        return "\n".join(prompt_lines)

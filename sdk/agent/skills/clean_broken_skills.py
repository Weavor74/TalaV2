"""Scans sdk/agent/skills/ for python files with invalid syntax or failed compilation and quarantines them"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Any, Dict

from sdk.agent.tools import register_tool


@register_tool(name="clean_broken_skills", description="Scans sdk/agent/skills/ for python files with invalid syntax or failed compilation and quarantines them")
async def clean_broken_skills() -> Dict[str, Any]:
    """Scans sdk/agent/skills/ for python files with invalid syntax or failed compilation and quarantines them"""
    skills_dir = Path("sdk/agent/skills")
    quarantine_dir = Path("scratch/quarantine")
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    scanned_count = 0
    removed_skills = []

    for py_file in sorted(skills_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        scanned_count += 1
        try:
            source = py_file.read_text(encoding="utf-8")
            ast.parse(source, filename=str(py_file))
            compile(source, str(py_file), "exec")
        except (SyntaxError, ValueError, TypeError, OSError):
            shutil.move(str(py_file), str(quarantine_dir / py_file.name))
            removed_skills.append(py_file.name)

    return {
        "scanned_count": scanned_count,
        "removed_skills": removed_skills,
        "status": "ok",
    }
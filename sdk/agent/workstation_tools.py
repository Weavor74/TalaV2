"""
workstation_tools.py -- the workstation toolset as the model sees it.

Thin registry wrappers over sdk/agent/workstation.py. Names are short and
plain on purpose: small local models pick tools by name. Imported for its
side effect from sdk/agent/tools.py. Grant them to a room agent with
scripts/grant_workstation_tools.py; the main chat offers the core five on
every non-casual turn (python/harness/tool_selection.py).
"""
from __future__ import annotations

from typing import Any

from sdk.agent import workstation as ws
from sdk.agent.tools import register_tool

WORKSTATION_TOOLS: tuple[str, ...] = (
    "run_shell", "read_file", "write_file", "list_dir", "search_files", "run_python",
    "delete_path", "install_python_package", "start_job", "job_status", "stop_job",
)
#: Offered on every non-casual main-chat turn while the workstation is enabled.
WORKSTATION_CORE_TOOLS: tuple[str, ...] = ("run_shell", "read_file", "write_file", "list_dir", "run_python")

#: Static on purpose: descriptions are also written into the tool HTML-NN
#: catalogue (scripts/generate_tool_nodes.py), which must not depend on the
#: machine it was generated on. Every result carries the real resolved path.
_WHERE = (" Your workspace is your own working folder on this computer (D:\\TalaWorkspace unless configured "
          "otherwise); relative paths are relative to it. Risky actions are held for the user's approval; "
          "deny-listed places are blocked.")


def _n(value: Any, default: Any = None) -> Any:
    """Small local models send "None" / "null" / "" for arguments they mean to omit."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "null", "undefined")):
        return default
    return value


def _s(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


@register_tool(
    name="run_shell",
    description=("Run a PowerShell command on your computer (Windows) and get stdout, stderr and the exit code. "
                 "Write PowerShell syntax. cwd defaults to the workspace. timeout_seconds defaults to 60; "
                 "use start_job for anything longer than a few minutes." + _WHERE),
    schema=_s({"command": _STR, "cwd": _STR, "timeout_seconds": _INT}, ["command"]),
)
async def run_shell(command: str, cwd: str | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
    return await ws.run_shell(command, cwd=_n(cwd), timeout_seconds=_n(timeout_seconds))


@register_tool(
    name="read_file",
    description="Read a text file from your computer, optionally a line range." + _WHERE,
    schema=_s({"path": _STR, "start_line": _INT, "end_line": _INT}, ["path"]),
)
async def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
    return await ws.read_file(path, start_line=_n(start_line), end_line=_n(end_line))


@register_tool(
    name="write_file",
    description=("Create or overwrite a text file on your computer (parent folders are created). "
                 "Set append=true to add to the end instead." + _WHERE),
    schema=_s({"path": _STR, "content": _STR, "append": _BOOL}, ["path", "content"]),
)
async def write_file(path: str, content: str, append: bool = False) -> dict[str, Any]:
    return await ws.write_file(path, content, append=append)


@register_tool(
    name="list_dir",
    description=("List a folder on your computer with sizes and modified times. recursive=true walks subfolders; "
                 "sort_by is name, size (largest first) or modified (newest first); files_only skips folders; "
                 "limit caps the rows returned." + _WHERE),
    schema=_s({"path": _STR, "recursive": _BOOL, "pattern": _STR, "sort_by": _STR, "limit": _INT,
               "files_only": _BOOL}, []),
)
async def list_dir(path: str = ".", recursive: bool = False, pattern: str = "*", sort_by: str = "name",
                   limit: int = 200, files_only: bool = False) -> dict[str, Any]:
    return await ws.list_dir(_n(path, "."), recursive=recursive, pattern=_n(pattern, "*"),
                             sort_by=_n(sort_by, "name"), limit=_n(limit, 200), files_only=files_only)


@register_tool(
    name="search_files",
    description=("Search files under a folder on your computer for text (or a regex with regex=true); "
                 "names_only=true matches file names instead of contents." + _WHERE),
    schema=_s({"query": _STR, "path": _STR, "pattern": _STR, "regex": _BOOL, "names_only": _BOOL}, ["query"]),
)
async def search_files(query: str, path: str = ".", pattern: str = "*", regex: bool = False,
                       names_only: bool = False) -> dict[str, Any]:
    return await ws.search_files(query, path=_n(path, "."), pattern=_n(pattern, "*"), regex=regex,
                                 names_only=names_only)


@register_tool(
    name="run_python",
    description=("Run Python 3 code (pass code) or a .py file (pass script) in your own Python venv on your "
                 "computer and get its output. cwd defaults to the workspace." + _WHERE),
    schema=_s({"code": _STR, "script": _STR, "args": {"type": "array", "items": _STR}, "cwd": _STR,
               "timeout_seconds": _INT}, []),
)
async def run_python(code: str | None = None, script: str | None = None, args: list | None = None,
                     cwd: str | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
    return await ws.run_python(code=_n(code), script=_n(script), args=_n(args), cwd=_n(cwd),
                               timeout_seconds=_n(timeout_seconds))


@register_tool(
    name="delete_path",
    description="Delete a file (or a folder with recursive=true) on your computer. Always needs approval." + _WHERE,
    schema=_s({"path": _STR, "recursive": _BOOL}, ["path"]),
)
async def delete_path(path: str, recursive: bool = False) -> dict[str, Any]:
    return await ws.delete_path(path, recursive=recursive)


@register_tool(
    name="install_python_package",
    description="pip install packages into your own Python venv. Always needs the user's approval." + _WHERE,
    schema=_s({"packages": {"type": "array", "items": _STR}}, ["packages"]),
)
async def install_python_package(packages: list) -> dict[str, Any]:
    return await ws.install_python_package(packages)


@register_tool(
    name="start_job",
    description=("Start a long-running PowerShell command in the background on your computer; returns a job_id. "
                 "Check it with job_status and end it with stop_job." + _WHERE),
    schema=_s({"command": _STR, "name": _STR, "cwd": _STR}, ["command"]),
)
async def start_job(command: str, name: str = "", cwd: str | None = None) -> dict[str, Any]:
    return await ws.start_job(command, name=_n(name, ""), cwd=_n(cwd))


@register_tool(
    name="job_status",
    description="Status and recent output of a background job (or all jobs when job_id is empty).",
    schema=_s({"job_id": _STR, "tail_lines": _INT}, []),
)
async def job_status(job_id: str = "", tail_lines: int = 40) -> dict[str, Any]:
    return await ws.job_status(_n(job_id, ""), tail_lines=_n(tail_lines, 40))


@register_tool(
    name="stop_job",
    description="Stop one of your own background jobs by job_id.",
    schema=_s({"job_id": _STR}, ["job_id"]),
)
async def stop_job(job_id: str) -> dict[str, Any]:
    return await ws.stop_job(job_id)

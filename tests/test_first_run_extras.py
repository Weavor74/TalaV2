"""
Optional extras must accumulate, not evict each other.

``uv sync`` removes extraneous packages by default, so two consecutive syncs
naming different extras leave only the last one installed. first_run.py ran
``--extra voice`` and then ``--extra transformers``, which is why an install
that printed both steps as done ended up with transformers and accelerate
present and openai-whisper and numba absent -- and therefore with the voice
capability the About panel advertises silently missing.

The failure is invisible at install time: both commands succeed, and the
second one's uninstall is not an error. Only a later import says so.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRST_RUN = ROOT / "scripts" / "first_run.py"


def _uv_sync_calls() -> list[list[str]]:
    """Every ``[uv, "sync", ...]`` argument list built in first_run.py."""
    tree = ast.parse(FIRST_RUN.read_text(encoding="utf-8"))
    calls: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if not (isinstance(first, ast.Name) and first.id == "uv"):
            continue
        literals = [e.value for e in node.elts[1:]
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if literals and literals[0] == "sync":
            calls.append(literals)
    return calls


def test_the_installer_actually_runs_uv_sync():
    """Guards the parser: an empty list would pass every test below."""
    assert _uv_sync_calls(), "no uv sync calls found -- has first_run.py moved?"


def test_every_optional_extra_sync_is_inexact():
    evicting = [
        call for call in _uv_sync_calls()
        if "--extra" in call
        and {"voice", "transformers"} & set(call)
        and "--inexact" not in call
    ]
    assert not evicting, (
        "these syncs install an optional extra while pruning whatever the "
        "previous one installed:\n  "
        + "\n  ".join(" ".join(c) for c in evicting)
    )


def _function_names_called_in(func_name: str) -> set[str]:
    """Plain-name calls made inside one top-level function of first_run.py."""
    tree = ast.parse(FIRST_RUN.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {
                n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
    raise AssertionError(f"{func_name}() not found in first_run.py")


def test_ffmpeg_provisioning_is_actually_called():
    """A provisioning function nothing invokes is the house failure mode: the
    capability looks installed because the code to install it exists."""
    assert "ensure_ffmpeg" in _function_names_called_in("main"), (
        "ensure_ffmpeg() is defined but main() never calls it, so a fresh "
        "install would not get ffmpeg"
    )


def test_first_run_never_asks_for_elevation():
    """It runs unattended from the Electron shell on first launch. A sudo or
    an elevated winget there either hangs on a prompt nobody sees or fails."""
    source = FIRST_RUN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("sudo"), f"elevation in: {node.value!r}"
    assert '"--scope", "machine"' not in source, "machine-scope install needs admin"


def test_the_base_sync_stays_exact():
    """The pruning default is right for the core set -- it is how a removed
    dependency actually leaves the environment. Only the optional extras, which
    are meant to stack, opt out of it."""
    base = [c for c in _uv_sync_calls()
            if not ({"voice", "transformers"} & set(c))]
    assert base, "expected a core sync without an optional extra"
    assert all("--inexact" not in c for c in base), (
        "the core sync should prune; otherwise removed dependencies linger"
    )

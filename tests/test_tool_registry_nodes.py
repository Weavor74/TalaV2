"""
The tool registry is a collection of HTML-NN, and stays one.

Steven's requirement (Q-33, UI-107):

    "The tool registry should be a collection of html-nn. If they are not we
    need to address that."

They were not: 88 registered tools and zero ``tool-*.html`` nodes. The
``sdk_agent_*.html`` nodes document *modules*, which is a different axis — a
module node cannot be retrieved by capability or referenced by a workflow step.

The Python registry remains what executes. These nodes are the catalogue, and
the risk they carry is drift: a tool added without a node is invisible to
retrieval, and a node whose tool was deleted is worse still, because a planner
will try to call something that is gone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.generate_tool_nodes import (
    NODE_PREFIX,
    _prose,
    _signature,
    discover_tool_modules,
)
from sdk.agent.documentation_nodes import _safe_node_id, resolve_active_nodes_dir
from sdk.agent.parser import parse_file

NODES = Path("nodes")


def _registered() -> dict[str, dict]:
    """
    The real capabilities, excluding anything a test registered.

    ToolRegistry is global and registration is an import side effect, so once
    pytest has imported sdk/agent/tests/test_ui_chat_tools.py its throwaway
    tools are in the registry too. Judged by the origin module rather than by
    what happens to be loaded: a fixture tool is not a capability, and
    demanding a catalogue node for one would be a permanent false failure.
    """
    for module in discover_tool_modules():
        try:
            __import__(module)
        except Exception:
            pass
    from sdk.agent.tools import ToolRegistry

    real = {}
    for tool in ToolRegistry._tools.values():
        module = getattr(tool.get("func"), "__module__", "") or ""
        if any(part.startswith("test") for part in module.split(".")):
            continue
        real[tool["name"]] = tool
    return real


def _node_id_for(name: str) -> str:
    return _safe_node_id(f"{NODE_PREFIX}{name.replace('_', '-')}")


def test_every_registered_tool_has_a_node():
    missing = sorted(
        name for name in _registered()
        if not (NODES / f"{_node_id_for(name)}.html").exists()
    )
    assert not missing, (
        f"{len(missing)} tool(s) have no HTML-NN — run "
        f"scripts/generate_tool_nodes.py: {missing}"
    )


def _tool_names_in_source() -> set[str]:
    """
    Every tool name declared in source, read from the files rather than the
    live registry.

    ToolRegistry is global mutable state and several tests add, replace and pop
    entries in it, so "what is registered right now" depends on test order --
    this check failed or passed depending on which tests ran first. What a node
    should mirror is the source declaration, which no test can perturb.
    """
    names: set[str] = set()
    pattern = re.compile(r'name\s*=\s*["\']([A-Za-z0-9_.\-]+)["\']')
    for module in discover_tool_modules():
        path = Path(*module.split(".")).with_suffix(".py")
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for block in text.split("@register_tool")[1:]:
            match = pattern.search(block[:400])
            if match:
                names.add(match.group(1))
        for block in text.split("ToolRegistry.register")[1:]:
            match = pattern.search(block[:400])
            if match:
                names.add(match.group(1))
    return names


def test_no_node_survives_its_tool():
    """A catalogue entry for a deleted tool is worse than a missing one: the
    planner will try to call it."""
    declared = _tool_names_in_source()
    assert declared, "no tool declarations found in source -- the scan is broken"

    expected = {_node_id_for(n) for n in declared}
    orphans = sorted(
        p.stem for p in NODES.glob(f"{NODE_PREFIX}*.html") if p.stem not in expected
    )
    assert not orphans, (
        f"{len(orphans)} node(s) describe tools that no longer exist — run "
        f"scripts/generate_tool_nodes.py --prune: {orphans}"
    )


def test_tool_module_discovery_finds_the_skills_directory():
    """A hardcoded module list missed sdk/agent/skills/, so those tools had no
    node and -- registration being an import side effect -- were invisible to
    the planner's catalogue as well."""
    modules = discover_tool_modules()
    assert any(m.startswith("sdk.agent.skills.") for m in modules)
    assert "sdk.agent.tools" in modules


def test_discovery_excludes_test_fixtures():
    """Test modules register throwaway tools that must never be catalogued."""
    for module in discover_tool_modules():
        assert "test" not in module.split("."), module


@pytest.mark.parametrize("name", sorted(_registered())[:12])
def test_each_node_carries_a_usable_call_signature(name):
    """The planner emitted empty tool_params for a year because the catalogue
    was name-and-prose only. A tool node that omits its arguments repeats that."""
    node = parse_file(str(NODES / f"{_node_id_for(name)}.html"))
    tool = (node.state_data or {}).get("tool") or {}

    assert tool.get("name") == name
    assert tool.get("signature", "").startswith(f"{name}(")
    assert "schema" in tool

    registered = _registered()[name]
    assert tool["schema"] == (registered.get("schema") or {})


def test_required_arguments_are_marked_in_the_signature():
    schema = {"type": "object",
              "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
              "required": ["a"]}
    assert _signature(schema) == "a*, b"


def test_the_prose_names_the_tool_and_its_arguments():
    """This is the text retrieval matches on when Tala looks for a capability
    she has not used before."""
    prose = _prose({
        "name": "do_thing",
        "description": "Does the thing.",
        "schema": {"type": "object",
                   "properties": {"target": {"type": "string"}},
                   "required": ["target"]},
    })
    assert "do_thing" in prose
    assert "Does the thing." in prose
    assert "target" in prose
    assert "required" in prose


def test_a_tool_with_no_arguments_says_so():
    prose = _prose({"name": "ping", "description": "Ping.", "schema": {}})
    assert "no arguments" in prose


def test_tool_nodes_are_recallable():
    """memory_authority 0 is a mute node; the catalogue would be unsearchable."""
    sample = sorted(NODES.glob(f"{NODE_PREFIX}*.html"))[:8]
    assert sample, "no tool nodes on disk"
    for path in sample:
        text = path.read_text(encoding="utf-8")
        authority = float(
            text.split('data-memory-authority="')[1].split('"')[0]
        )
        assert authority > 0, f"{path.stem} is mute"


def test_tool_nodes_never_claim_execution_authority():
    """These are catalogue entries. The Python registry is what executes, and a
    document that claims otherwise invites the DAG engine to act on it."""
    for path in sorted(NODES.glob(f"{NODE_PREFIX}*.html"))[:8]:
        text = path.read_text(encoding="utf-8")
        authority = float(
            text.split('data-execution-authority="')[1].split('"')[0]
        )
        assert authority == 0.0, f"{path.stem} claims execution authority"


def test_state_extra_cannot_forge_reserved_fields(tmp_path, monkeypatch):
    """state_extra exists to carry a tool's schema, not to let a caller
    overwrite the metadata block or the fingerprint."""
    import sdk.agent.documentation_nodes as dn
    monkeypatch.setattr(dn, "resolve_active_nodes_dir", lambda: tmp_path)

    dn.create_documentation_node(
        node_id="tool-forgery-check",
        title="Real Title",
        doc_target="forgery_check",
        semantic_memory="<p>prose</p>",
        node_type="function-doc",
        state_extra={"metadata": {"author": "attacker"}, "title": "Fake",
                     "tool": {"name": "forgery_check"}},
    )
    state = parse_file(str(tmp_path / "tool-forgery-check.html")).state_data
    assert state["title"] == "Real Title"
    assert state["metadata"]["author"] == "tala-system"
    assert state["tool"]["name"] == "forgery_check"

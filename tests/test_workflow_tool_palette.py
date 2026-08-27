"""
Every tool is available in the workflow builder.

Steven's point 2 on the workflow panel (UI-059): *"Every tool created in the
html-nn should be available here."* The palette was five static
``<div data-type>`` elements -- trigger, auth, email, custom_script, output --
while the registry held ninety tools and none of them appeared.

The catalogue is served from the ``tool-*.html`` nodes rather than the Python
registry, because a palette built from the registry would leave the nodes
decorative, which is the thing UI-107 just fixed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

INDEX = Path("renderer/index.html")
RENDERER = Path("renderer/renderer.js")


# -- the catalogue RPC --------------------------------------------------------

def _catalogue(params=None):
    from python.harness.server import handle_tools_catalogue
    raw = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        handle_tools_catalogue(params or {}, 1)
    )
    return json.loads(raw)["result"]


@pytest.mark.asyncio
async def test_the_catalogue_serves_every_tool_node():
    from python.harness.server import handle_tools_catalogue
    result = json.loads(await handle_tools_catalogue({}, 1))["result"]

    on_disk = len(list(Path("nodes").glob("tool-*.html")))
    assert result["count"] == on_disk, "the palette would be missing tools"
    assert result["unreadable"] == 0


@pytest.mark.asyncio
async def test_each_entry_carries_what_a_step_needs_to_call_it():
    from python.harness.server import handle_tools_catalogue
    tools = json.loads(await handle_tools_catalogue({}, 1))["result"]["tools"]

    entry = next(t for t in tools if t["name"] == "user_profile_lookup")
    assert entry["signature"] == "user_profile_lookup(section*)"
    assert entry["required"] == ["section"]
    assert "section" in entry["properties"]
    assert entry["schema"]["properties"]["section"]["type"] == "string"


@pytest.mark.asyncio
async def test_the_description_is_the_tool_s_own_not_the_doc_boilerplate():
    """Every documentation node's description field is stamped "Program
    documentation for X", which tells a user nothing about what the tool does."""
    from python.harness.server import handle_tools_catalogue
    tools = json.loads(await handle_tools_catalogue({}, 1))["result"]["tools"]

    for entry in tools:
        assert not entry["description"].startswith("Program documentation for"), entry["name"]


@pytest.mark.asyncio
async def test_the_catalogue_can_be_filtered():
    from python.harness.server import handle_tools_catalogue
    result = json.loads(await handle_tools_catalogue({"query": "profile"}, 1))["result"]

    assert result["count"] >= 1
    assert all(
        "profile" in t["name"].lower() or "profile" in t["description"].lower()
        for t in result["tools"]
    )


@pytest.mark.asyncio
async def test_the_catalogue_is_read_from_the_nodes():
    """Not from the Python registry -- otherwise the nodes are decorative."""
    import inspect
    from python.harness import server

    source = inspect.getsource(server.handle_tools_catalogue)
    assert 'glob("tool-*.html")' in source
    assert "ToolRegistry" not in source


# -- the palette markup -------------------------------------------------------

def test_the_palette_has_a_tool_section():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="workflow-tool-palette"' in html
    assert 'id="workflow-tool-search"' in html, "ninety tools need a filter"
    assert 'id="workflow-tool-count"' in html


def test_a_tool_step_can_be_configured():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="config-tool-fields"' in html
    assert 'id="prop-tool-args"' in html
    assert 'id="prop-tool-name"' in html
    # and the config group follows the existing convention so the
    # hide-all-then-show-one logic covers it
    assert 'id="config-tool-fields" class="dynamic-config-fields"' in html


def test_the_renderer_loads_filters_and_drops_tools():
    js = RENDERER.read_text(encoding="utf-8")
    assert "tools.catalogue" in js, "the palette is not fed from the catalogue"
    assert "loadToolPalette" in js
    assert "renderToolPalette" in js
    assert "data-type=\"tool\"" in js, "dropped tools must become tool nodes"


def test_a_dropped_tool_carries_its_name_and_arguments():
    """config.tool and config.args are what the executor reads."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("addNodeAt(x, y) {", 1)[1].split("renderCanvasNodes()", 1)[0]
    assert "tool: this._draggedTool.name" in block
    assert "args: this.defaultArgsFor(this._draggedTool)" in block


def test_the_properties_panel_shows_the_live_signature():
    """A signature stored on the node goes stale when the tool changes, which
    is exactly how a step ends up sending arguments that no longer exist."""
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("} else if (node.type === 'tool') {", 1)[1][:900]
    assert "this._tools || []" in block
    assert "known?.signature" in block


def test_unparseable_arguments_are_not_saved_as_empty():
    """Silently writing {} would strip the user's work and run the step with no
    arguments at all."""
    js = RENDERER.read_text(encoding="utf-8")
    # the method definition, not the call sites in the input listeners
    save = js.split("\n  saveNodeProperties() {", 1)[1]
    block = save.split("} else if (node.type === 'tool') {", 1)[1][:1400]
    assert "catch" in block
    assert "Not saved" in block
    assert "node.config.args = parsed" in block


def test_tool_names_are_escaped_into_the_palette():
    js = RENDERER.read_text(encoding="utf-8")
    block = js.split("renderToolPalette() {", 1)[1].split("defaultArgsFor", 1)[0]
    assert "escapeHtml(t.name)" in block
    assert "escapeHtml((t.description" in block


def test_the_five_original_node_types_survive():
    """The tool palette is an addition, not a replacement -- trigger and output
    are structural and have no tool equivalent."""
    html = INDEX.read_text(encoding="utf-8")
    for node_type in ("trigger", "auth", "email", "custom_script", "output"):
        assert f'data-type="{node_type}"' in html

"""
The last two unreachable capabilities from the reachability audit.

  swarm control   swarm.pause / resume / cancel are thin wrappers over
                  task.control and nothing called any of them. The task UI only
                  ever offered Abort, so pause and resume were unreachable for
                  tasks too. Being able to start a swarm without being able to
                  stop it got worse the moment Tala could start one herself.

  mcp discovery   mcp.discover existed only as an RPC and a tool, so Tala could
                  scan the machine and the operator could not.
"""
import re
from pathlib import Path

import pytest

from python.harness.server import _HANDLERS
from sdk.agent import agent_authoring
from sdk.agent.tools import ToolRegistry

RENDERER = Path("renderer/renderer.js")
INDEX = Path("renderer/index.html")
SERVER = Path("python/harness/server.py")


# --- swarm control is reachable by both parties -----------------------------


def test_control_swarm_is_a_registered_tool():
    """start_swarm shipped before anything could stop what it started."""
    assert "control_swarm" in {t["name"] for t in ToolRegistry.get_tools()}


@pytest.mark.parametrize("action", ["pause", "resume", "abort"])
@pytest.mark.asyncio
async def test_control_swarm_accepts_the_real_actions(action):
    """Rejected for a missing id rather than a bad action, which proves the
    action passed validation."""
    result = await agent_authoring.control_swarm("", action)

    assert result["success"] is False
    assert "parent_task_id" in result["error"]


@pytest.mark.asyncio
async def test_control_swarm_refuses_an_unknown_action():
    result = await agent_authoring.control_swarm("some-task", "obliterate")

    assert result["success"] is False
    assert "action" in result["error"]


@pytest.mark.parametrize("action", ["pause", "resume", "abort"])
def test_the_task_ui_offers_every_action_the_harness_supports(action):
    """task.control implements pause, resume and abort; the UI offered only
    abort, so two of the three were unreachable from anywhere."""
    renderer = RENDERER.read_text(encoding="utf-8")

    assert f'data-task-action="{action}"' in renderer


def test_the_ui_routes_all_three_through_task_control():
    """
    Every task action must reach task.control.

    Originally pinned to the exact array literal, which meant adding the
    Mission Control approve/reject actions broke a test whose real subject --
    that these three are routed -- was still satisfied. Asserts membership now,
    so the guard can grow without a false failure.
    """
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "async controlTask(id, action)" in renderer
    match = re.search(r"\[([^\]]*)\]\.includes\(action\)", renderer)
    assert match, "no dispatch guard of the form [...].includes(action) was found"
    routed = {a.strip().strip("'\"") for a in match.group(1).split(",")}
    for action in ("abort", "pause", "resume"):
        assert action in routed, f"{action} is not routed through controlTask"


def test_only_the_irreversible_action_confirms():
    """Abort cascades to children and cannot be undone. Pause and resume are
    reversible and their value is being quick."""
    renderer = RENDERER.read_text(encoding="utf-8")
    block = renderer.split("async controlTask(id, action)", 1)[1].split("async saveTask(", 1)[0]

    assert "if (action === 'abort')" in block
    assert "confirm(" in block
    assert block.index("if (action === 'abort')") < block.index("confirm(")


def test_pause_cascades_to_child_nodes():
    """A swarm parent must take its dispatched nodes with it, which is what
    makes controlling a swarm through task.control correct at all."""
    # handle_task_control moved to task_rpc.py in the size-ratchet cut-down;
    # the cascade requirement follows the code.
    server = Path("python/harness/task_rpc.py").read_text(encoding="utf-8")
    block = server.split("async def handle_task_control", 1)[1][:1400]

    assert "parent_id = $1" in block


# --- MCP discovery is reachable by the operator -----------------------------


def test_the_scan_has_a_button():
    markup = INDEX.read_text(encoding="utf-8")

    for element in ('id="btn-mcp-discover"', 'id="mcp-discovered"', 'id="mcp-discovered-list"'):
        assert element in markup, f"missing {element}"


def test_the_panel_calls_discover_and_adopt():
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "'mcp.discover'" in renderer
    assert "async discoverServers()" in renderer
    assert "async adoptDiscovered(" in renderer


def test_adopting_is_a_deliberate_per_server_act():
    """The scan finds servers; it must not connect them. Adopting one runs a
    subprocess with the operator's privileges."""
    renderer = RENDERER.read_text(encoding="utf-8")
    scan = renderer.split("async discoverServers()", 1)[1].split("async adoptDiscovered(", 1)[0]
    adopt = renderer.split("async adoptDiscovered(", 1)[1].split("async connectServer(", 1)[0]

    assert "mcp.connect_server" not in scan, "the scan connects to something"
    assert "confirm(" in adopt, "adopting must be confirmed"
    assert "mcp.connect_server" in adopt


def test_a_server_needing_secrets_says_so_before_adoption():
    """The scan never carries an env block, so such a server is adopted without
    its secrets and would fail at the first tool call."""
    renderer = RENDERER.read_text(encoding="utf-8")
    adopt = renderer.split("async adoptDiscovered(", 1)[1].split("async connectServer(", 1)[0]

    assert "has_env_secrets" in adopt
    assert "NOT copied" in adopt


def test_discovered_fields_are_escaped():
    """Names and commands come from config files written by other tools."""
    renderer = RENDERER.read_text(encoding="utf-8")
    scan = renderer.split("async discoverServers()", 1)[1].split("async adoptDiscovered(", 1)[0]

    for interpolation in re.findall(r"\$\{([^}]*d\.[a-z_]+[^}]*)\}", scan):
        if "escapeHtml" in interpolation or "d.has_env_secrets" in interpolation or "d.already_registered" in interpolation:
            continue
        raise AssertionError(f"discovered field interpolated unescaped: ${{{interpolation}}}")


def test_discover_is_dispatchable():
    assert "mcp.discover" in _HANDLERS

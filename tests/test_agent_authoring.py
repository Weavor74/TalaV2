"""
Tala composing multi-agent work, not just executing it.

The execution half was already wired: a TopologyNode carries an agent_id, the
dispatcher writes it onto the task, and DAGExecutionEngine resolves that agent's
system prompt. What was missing was the authoring half, and only for her --
agent.save_agent, swarm.topology.create and swarm.start are RPC methods Mission
Control calls, and none appeared in the tool registry.

Same shape as the cron scheduler that ran from boot with no UI calling it: a
working mechanism with no entry point for the party who should drive it. There
the missing party was the operator; here it is the agent, which matters more,
because a self-improvement loop the agent cannot reach is not closed.
"""
import inspect
from pathlib import Path

import pytest

from sdk.agent import agent_authoring  # noqa: F401  (registers the tools)
from sdk.agent.agent_authoring import MAX_TOPOLOGY_NODES
from sdk.agent.tools import ToolRegistry


@pytest.fixture(scope="module")
def tool_names():
    return {t["name"] for t in ToolRegistry.get_tools()}


# --- the tools exist and are reachable --------------------------------------


@pytest.mark.parametrize("name", [
    "list_agents", "create_agent", "create_swarm_topology", "start_swarm",
])
def test_authoring_tool_is_registered(tool_names, name):
    """Registered, not merely defined -- an unregistered handler is this
    codebase's most repeated defect, and selection can only offer what the
    registry holds."""
    assert name in tool_names


def test_the_tools_carry_argument_schemas(name="create_swarm_topology"):
    """The planner emits empty tool_params when it cannot see argument names;
    fixing that took the agent benchmarks from 2/6 to 4/6."""
    entry = ToolRegistry._tools[name.lower()]
    properties = (entry.get("schema") or {}).get("properties", {})

    assert {"topology_id", "name", "nodes", "edges"} <= set(properties)


# --- a persona is a voice, not a permission ---------------------------------


def test_create_agent_cannot_grant_authority():
    """An agent record holds name, role, system_prompt and tag. If it ever
    gains a model override or tool grant, direct creation by the agent stops
    being defensible and this test should fail to say so."""
    source = Path("sdk/agent/agent_authoring.py").read_text(encoding="utf-8")
    body = source.split("async def create_agent", 1)[1].split("MAX_TOPOLOGY_NODES", 1)[0]

    for forbidden in ("execution_authority", "model", "provider", "tool_authority"):
        assert f'"{forbidden}"' not in body, (
            f"create_agent writes {forbidden}, which would make a persona a privilege grant"
        )


def test_create_agent_signature_is_persona_only():
    """birthdate/birth_hour are persona too: they set the agent's natal chart
    for the astro-emotion layer (voice and mood colour), and grant nothing —
    the authority-shaped names stay forbidden by the test above."""
    params = set(inspect.signature(agent_authoring.create_agent).parameters)

    assert params == {"name", "system_prompt", "role", "tag", "birthdate", "birth_hour"}


# --- refusals ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_persona_with_no_prompt_is_refused():
    result = await agent_authoring.create_agent(name="Nameless", system_prompt="   ")

    assert result["success"] is False
    assert "system_prompt" in result["error"]


@pytest.mark.asyncio
async def test_an_unknown_gate_condition_is_refused():
    """An edge naming a rule the verifier does not implement can never pass, so
    that branch stalls silently on every run -- the failure the editor's gate
    dropdown was generated from KNOWN_RULES to prevent."""
    result = await agent_authoring.create_swarm_topology(
        "probe-gate", "Probe",
        [{"id": "a", "role": "worker", "description": "d"}],
        [{"from_node": "a", "to_node": "b", "condition": "definitely_not_a_rule"}],
    )

    assert result["success"] is False
    assert "never pass" in result["error"]


@pytest.mark.asyncio
async def test_an_oversized_topology_is_refused():
    """Every node becomes a real task and a real model call."""
    nodes = [{"id": f"n{i}", "role": "worker", "description": "d"} for i in range(MAX_TOPOLOGY_NODES + 3)]

    result = await agent_authoring.create_swarm_topology("probe-big", "Big", nodes, [])

    assert result["success"] is False
    assert "too many" in result["error"]


@pytest.mark.asyncio
async def test_an_empty_node_list_is_refused():
    result = await agent_authoring.create_swarm_topology("probe-empty", "Empty", [], [])

    assert result["success"] is False


@pytest.mark.asyncio
async def test_starting_an_unknown_topology_says_how_to_fix_it():
    result = await agent_authoring.start_swarm("do something", "no-such-topology-xyz")

    assert result["success"] is False
    assert "create_swarm_topology" in result["error"]


@pytest.mark.asyncio
async def test_starting_without_a_goal_is_refused():
    result = await agent_authoring.start_swarm("  ", "anything")

    assert result["success"] is False
    assert "goal" in result["error"]


# --- the authored topology is validated the way the loader validates ---------


def test_topologies_are_validated_through_the_loader_path():
    """Written only after topology_from_spec accepts it, so a topology that
    would not load back is refused rather than persisted -- the 'saved
    successfully and then vanished from the list' failure."""
    source = Path("sdk/agent/agent_authoring.py").read_text(encoding="utf-8")
    block = source.split("async def create_swarm_topology", 1)[1]

    assert "topology_from_spec(spec)" in block
    assert block.index("topology_from_spec(spec)") < block.index("write_topology_node(")


def test_start_swarm_reloads_before_giving_up():
    """A topology authored moments earlier is on disk but not in the registry,
    which loads at startup -- without a reload, create-then-start fails in a
    single turn."""
    source = Path("sdk/agent/agent_authoring.py").read_text(encoding="utf-8")
    block = source.split("async def start_swarm", 1)[1]

    assert "TopologyRegistry.reset()" in block

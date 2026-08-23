"""
Third cloud review, on the largest slice.

Three findings. Two are the same defect seen from different angles: a value or a
capability was changed in one place and the places that check it were left
behind, so the system rejected or withheld its own correct output.

  research tools   _BUILTIN_RESEARCH_TOOL_SPECS exists so swapping to the native
                   protocol cannot lose web search. The selection index was built
                   without them, so they could never be scored, were not in the
                   core set, and were withheld on every turn for any
                   budget-limited provider -- while the XML docs that described
                   them were stripped in the same request.

  doc authority    memory_authority was raised 0.00 -> 0.60 in the generators and
                   one validator. Two other validators kept demanding 0.00, so
                   every node the generator wrote was reported invalid by the
                   generator itself, and the tool the model calls could not
                   produce a recallable node at all.

  streamed think   the native /api/chat streaming branch read message.thinking
                   into a local and dropped it, losing the reasoning the native
                   path exists to preserve.
"""
import os
import tempfile
from pathlib import Path

import pytest

from python.harness.server import _BUILTIN_RESEARCH_TOOL_SPECS, _ollama_native_tool_specs
from python.harness.tool_selection import ToolIndex, select_specs
from sdk.agent.documentation_nodes import DOC_NODE_MEMORY_AUTHORITY
from sdk.agent.tools import ToolRegistry

SERVER = Path("python/harness/server.py")
DOC_NODES = Path("sdk/agent/documentation_nodes.py")
TOOLS = Path("sdk/agent/tools.py")


# --- research tools must be selectable --------------------------------------


@pytest.fixture(scope="module")
def specs_and_index():
    dyn = ToolRegistry.get_tools()
    specs = list(_BUILTIN_RESEARCH_TOOL_SPECS) + _ollama_native_tool_specs(dyn)
    index = ToolIndex(dyn + [
        {
            "name": s["function"]["name"],
            "description": s["function"].get("description", ""),
            "schema": s["function"].get("parameters", {}),
        }
        for s in _BUILTIN_RESEARCH_TOOL_SPECS
    ])
    return specs, index


@pytest.mark.parametrize("prompt,expected", [
    ("search the web for the webb telescope", "research.search"),
    ("crawl these urls and summarise them into a node", "research.crawl_and_summarize"),
])
def test_research_tools_can_be_selected_at_the_smallest_budget(specs_and_index, prompt, expected):
    """Budget 8 is the embedded tier -- the tightest. These were withheld at
    every budget before, because the index could not score a tool it had never
    been given."""
    specs, index = specs_and_index

    kept, _withheld = select_specs(specs, prompt, index, 8)

    assert expected in [k["function"]["name"] for k in kept]


def test_the_index_is_built_from_the_builtin_specs_too(specs_and_index):
    """The mechanism, asserted directly: a name the index has never seen cannot
    be returned by score(), and neither research tool is in DEFAULT_CORE_TOOLS."""
    _specs, index = specs_and_index

    scored = index.score("crawl and summarise a web page into a neural node")

    assert "research.crawl_and_summarize" in scored


def test_server_indexes_the_builtin_specs():
    source = SERVER.read_text(encoding="utf-8")
    block = source.split("tool_index = ToolIndex(", 1)[1][:600]

    assert "_BUILTIN_RESEARCH_TOOL_SPECS" in block, (
        "the index is built from dyn_tools alone, so the research tools can "
        "never be selected"
    )


# --- documentation authority: one value, checked everywhere -----------------


def test_a_freshly_written_node_validates():
    """The generator rejected its own output: it wrote 0.60 and its post-write
    check demanded 0.00, so every node reported validation_passed=False."""
    from sdk.agent.documentation_nodes import create_documentation_node

    os.environ["TALA_USER_DATA"] = tempfile.mkdtemp(prefix="tala-r3-")
    result = create_documentation_node(
        node_id="probe-round3",
        title="Probe",
        doc_target="probe.round3",
        semantic_memory="A body of prose.",
        source_files=["sdk/agent/documentation_nodes.py"],
    )

    assert result["validation_passed"] is True, result["errors"]
    assert not result["errors"]


def test_every_check_compares_against_the_shared_constant():
    """Four sites have to agree. When the value moved, only two did."""
    doc_source = DOC_NODES.read_text(encoding="utf-8")

    assert f'data-memory-authority="{DOC_NODE_MEMORY_AUTHORITY:.2f}"' in doc_source
    assert "parsed.memory_authority != DOC_NODE_MEMORY_AUTHORITY" in doc_source
    assert "if parsed.memory_authority <= 0.0:" in doc_source  # verify_documentation_nodes


def test_the_model_facing_tool_can_produce_a_recallable_node():
    """It defaulted to 0.00 and hard-refused anything else, so the tool Tala
    calls to document herself could only ever write a mute node."""
    tools_source = TOOLS.read_text(encoding="utf-8")

    assert "memory_authority: float | None = None" in tools_source, "the default must not be 0.0"
    assert "DOC_NODE_MEMORY_AUTHORITY" in tools_source
    assert 'raise ValueError("documentation nodes require memory_authority 0.00")' not in tools_source


def test_documentation_still_cannot_execute():
    """The zero that was always correct stays zero."""
    doc_source = DOC_NODES.read_text(encoding="utf-8")

    assert "parsed.execution_authority != 0.0" in doc_source
    assert 'data-execution-authority="0.00"' in doc_source


# --- streamed reasoning -----------------------------------------------------


def test_native_streaming_wraps_thinking():
    """Reasoning arriving in message.thinking is the stated reason the native
    path exists; the streaming branch read it into a local and dropped it."""
    source = SERVER.read_text(encoding="utf-8")
    block = source.split('_msg = _obj.get("message") or {}', 1)[1].split("if _obj.get(\"done\")", 1)[0]

    assert '_msg.get("thinking")' in block
    assert "<thinking>" in block
    assert "</thinking>" in block


def test_reasoning_is_closed_before_the_first_visible_token():
    """Otherwise the reasoning leaks into the reply, which is the failure the
    tag convention exists to prevent."""
    source = SERVER.read_text(encoding="utf-8")
    block = source.split('_msg = _obj.get("message") or {}', 1)[1].split("if _obj.get(\"done\")", 1)[0]

    assert "if _chunk and in_reasoning:" in block
    assert 'in_reasoning = False' in block

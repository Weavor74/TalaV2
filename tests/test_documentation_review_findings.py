"""
Three faults found reviewing the documentation system, pinned so they stay fixed.

All three were real, all three reproduced exactly as reported, and each is a
different way for a maintenance system to look like it is working.

1. **An import moved the application's data directory.** The module documenter
   set ``TALA_USER_DATA`` to the checkout at import time. That was harmless
   while the only caller was its own command line, and became a fault the
   moment the harness began importing it for the daily refresh: every later
   read of that variable — not just documentation writes — pointed somewhere
   the operator had not configured, for the life of the process. An import
   must not reconfigure its importer.

2. **A refreshed hash did not mean a refreshed explanation.** The digest
   covers the whole file; the derived prose covers docstrings and public
   names. Change a function body and leave its docstring alone — swap an
   Amazon call for an API key — and the digest moves, the node is regenerated,
   and the description comes out byte-identical, still describing Amazon. The
   node was then honestly source-current and quietly wrong, which is worse
   than being marked stale, because stale at least says so.

3. **The deference notice never reached recall.** It sat in its own
   ``<section>``, and the parser's semantic-memory extraction reads only the
   element with id ``semantic-memory``. So a statement whose entire purpose
   was to be read by a model reasoning from the node was invisible to the
   path that feeds one. Well recorded, and useless.

The third is the pattern worth remembering: storing something correctly is
not the same as putting it where it will be read. Fixing (2) required
applying that lesson immediately — the behaviour warning goes in the recalled
text, not only in an attribute.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _nodes_dir() -> Path:
    """
    Where nodes actually live for this process.

    conftest pins TALA_USER_DATA to a temporary store so the suite never
    writes into the real one. A test that reads ROOT/"nodes" while the code
    under test writes to the temporary store is comparing two different
    files and will fail for a reason that has nothing to do with the code.
    """
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    return resolve_active_nodes_dir()


# ---------------------------------------------------------------------------
# 1. An import must not reconfigure its importer
# ---------------------------------------------------------------------------


def test_importing_the_documenter_leaves_the_data_root_alone():
    """
    Run in a subprocess: the damage is a module-level side effect, and this
    process has already imported the module.
    """
    sentinel = "/configured/elsewhere"
    script = (
        "import os, sys;"
        f"os.environ['TALA_USER_DATA'] = {sentinel!r};"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "import scripts.generate_sdk_agent_nodes;"
        "print(os.environ['TALA_USER_DATA'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == sentinel, (
        "importing the module rewrote TALA_USER_DATA; the harness imports it "
        f"for the daily refresh. Got: {result.stdout.strip()!r}"
    )


def test_the_command_line_still_pins_the_nodes_directory():
    """
    The pin was not wrong, only misplaced. A bare run on a machine with that
    variable set would otherwise write its nodes somewhere else entirely.
    """
    source = (ROOT / "scripts" / "generate_sdk_agent_nodes.py").read_text(
        encoding="utf-8", errors="replace"
    )
    main_body = source.split("def main()", 1)[1]
    assert "_pin_nodes_dir_to_repo()" in main_body


# ---------------------------------------------------------------------------
# 2. Source-current is not description-verified
# ---------------------------------------------------------------------------


def test_a_body_change_does_not_alter_the_derived_prose():
    """
    Not a defect to fix — a limit to be honest about. The documenter reads
    names and docstrings; it does not interpret bodies. Pinning it here is
    what makes the warning below necessary rather than optional.
    """
    sys.path.insert(0, str(ROOT))
    from scripts.generate_sdk_agent_nodes import _describe_module, _semantic_memory

    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "thing.py"
    before_src = '"""A module."""\n\n\ndef fetch(url):\n    """Fetch the thing."""\n    return call_amazon(url)\n'
    tmp.write_text(before_src, encoding="utf-8")
    before = _semantic_memory("t.thing", "t/thing.py", _describe_module(tmp))

    tmp.write_text(before_src.replace("call_amazon", "call_with_api_key"), encoding="utf-8")
    after = _semantic_memory("t.thing", "t/thing.py", _describe_module(tmp))

    assert before == after, (
        "the documenter now reads bodies — the behaviour warning may be "
        "unnecessary, and this test should be revisited rather than deleted"
    )


@pytest.mark.asyncio
async def test_a_behaviour_change_is_marked_survives_regeneration_and_then_clears():
    """
    The reviewer's scenario end to end, plus the hole found while fixing it.

    One test rather than three because it is one property with three moments,
    and splitting it made the parts fight: each needs the same module edited
    and restored, so run separately they hand each other a store in a state
    the next one did not expect.

    The moments:

    * a function body changes while its docstring does not — the digest moves,
      the node is rewritten, and the prose comes out identical, so the node is
      honestly source-current and quietly wrong. It must say so, in the text a
      model actually recalls;
    * a routine bulk regeneration must not launder that warning. Every node is
      rewritten from the template, so without care the warning survives exactly
      until the next regeneration — the one thing guaranteed to happen;
    * and it must clear once the description genuinely catches up, or it is
      noise that teaches you to ignore it.
    """
    sys.path.insert(0, str(ROOT))
    from python.harness.doc_maintenance import refresh_stale_documentation
    from scripts.generate_sdk_agent_nodes import DOCUMENTED_PACKAGES, _document_package
    from sdk.agent.documentation_nodes import UNVERIFIED_MARK

    harness = next(p for p in DOCUMENTED_PACKAGES if p["module_prefix"] == "python.harness")
    target = ROOT / "python" / "harness" / "vps_mode.py"
    node = _nodes_dir() / "python_harness_vps_mode.html"
    original = target.read_bytes()

    def recalled() -> str:
        body = re.search(
            r'<section id="semantic-memory">(.*?)</section>',
            node.read_text(encoding="utf-8", errors="replace"), re.DOTALL,
        )
        return body.group(1) if body else ""

    try:
        source = original.decode("utf-8")
        edited = source.replace("return mode.is_enabled()", "return bool(mode.is_enabled())", 1)
        assert edited != source, "the fixture line has moved; pick another body to change"
        target.write_bytes(edited.encode("utf-8"))

        result = await refresh_stale_documentation()
        flagged = {u["node_id"] for u in result.get("description_unverified", [])}
        assert "python_harness_vps_mode" in flagged, (
            "a behaviour change produced identical prose and nothing said so"
        )
        assert UNVERIFIED_MARK in recalled(), (
            "the warning is not in the text a model actually recalls — the same "
            "mistake as the reconciliation notice"
        )

        _document_package(harness, dry_run=False)
        assert UNVERIFIED_MARK in recalled(), (
            "a bulk regeneration cleared a warning that was still true"
        )
    finally:
        target.write_bytes(original)
        _document_package(harness, dry_run=False)

    assert UNVERIFIED_MARK not in recalled(), (
        "the warning outlived the condition it described"
    )


# ---------------------------------------------------------------------------
# 3. Recorded is not the same as read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_id", [
    "system-tala-agent-architecture",
    "system-neural-node-v2-schema",
    "ui-view-agent-builder",
])
def test_the_deference_reaches_the_recalled_text(node_id):
    from sdk.agent.parser import parse_file

    parsed = parse_file(_nodes_dir() / f"{node_id}.html")
    recalled = parsed.semantic_memory

    assert "system-architecture-contract" in recalled, (
        f"{node_id} defers to the contract in a section the parser does not "
        "read, so nothing reasoning from this node ever learns it"
    )
    assert "authoritative" in recalled


def test_the_deference_is_not_stacked_by_repeated_runs():
    for node_id in ("system-tala-agent-architecture", "ui-view-agent-builder"):
        html = (_nodes_dir() / f"{node_id}.html").read_text(
            encoding="utf-8", errors="replace"
        )
        assert html.count("[Reconciled]") == 1, node_id
        assert html.count('id="contract-reconciliation"') == 1, node_id


def test_the_recalled_deference_is_short_enough_to_lead_with():
    """
    It is prepended to every recall of these nodes. The full block stays in
    its own section; what goes into the context is one sentence, or it
    crowds out the document it is annotating.
    """
    from sdk.agent.parser import parse_file

    parsed = parse_file(_nodes_dir() / "system-tala-agent-architecture.html")
    first = parsed.semantic_memory.strip().split("\n")[0]
    assert first.startswith("[Reconciled]")
    assert len(first) < 500, f"the deference notice is {len(first)} chars"

"""
Regenerating a documentation node with unchanged input must change nothing.

create_documentation_node has an unchanged check -- `if previous_content == html`
-- but generated_at was stamped with datetime.now() and embedded in that html, so
the two strings always differed by at least the timestamp. The branch could never
be reached. Every regeneration rewrote every node and reported it as updated.

Seen as nodes/program-doc-chat-panel.html sitting modified across the whole
session with a two-line diff of nothing but a timestamp, while
data-source-fingerprint -- the actual content identity -- was byte-for-byte
identical.
"""
import re
import time
from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import create_documentation_node

ARGS = dict(
    node_id="probe-idempotence-node",
    title="Probe",
    doc_target="probe.idempotence",
    semantic_memory="A stable body of prose that does not change between runs.",
    source_files=["sdk/agent/documentation_nodes.py"],
)


@pytest.fixture
def isolated_nodes(tmp_path, monkeypatch):
    """resolve_active_nodes_dir reads TALA_USER_DATA; without pinning it the
    generator writes into the real nodes directory."""
    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    return tmp_path


def test_second_generation_with_identical_input_changes_nothing(isolated_nodes):
    first = create_documentation_node(**ARGS)
    path = Path(first["path"])
    before = path.read_text(encoding="utf-8")

    # A different wall-clock second, which is exactly what used to defeat the
    # comparison.
    time.sleep(1.1)
    second = create_documentation_node(**ARGS)

    assert first["created"] is True
    assert second["unchanged"] is True, "an unchanged node must not report as updated"
    assert second["updated"] is False
    assert path.read_text(encoding="utf-8") == before, "the file was rewritten"


def test_the_timestamp_is_reused_rather_than_restamped(isolated_nodes):
    """The mechanism, asserted directly: identical fingerprint means the old
    generated_at is carried forward."""
    first = create_documentation_node(**ARGS)
    path = Path(first["path"])
    stamp = re.search(r'data-generated-at="([^"]*)"', path.read_text(encoding="utf-8")).group(1)

    time.sleep(1.1)
    create_documentation_node(**ARGS)
    restamped = re.search(r'data-generated-at="([^"]*)"', path.read_text(encoding="utf-8")).group(1)

    assert restamped == stamp


def test_changed_content_still_rewrites_and_restamps(isolated_nodes):
    """The fix must not make the node stale: a real content change has to produce
    a new fingerprint, a new timestamp and a write."""
    first = create_documentation_node(**ARGS)
    path = Path(first["path"])
    original = path.read_text(encoding="utf-8")
    original_stamp = re.search(r'data-generated-at="([^"]*)"', original).group(1)

    time.sleep(1.1)
    changed = dict(ARGS, semantic_memory="Genuinely different prose describing something else.")
    second = create_documentation_node(**changed)
    updated_text = path.read_text(encoding="utf-8")

    assert second["unchanged"] is not True
    assert updated_text != original
    assert re.search(r'data-generated-at="([^"]*)"', updated_text).group(1) != original_stamp


def test_fingerprint_is_what_decides(isolated_nodes):
    """Identity is the source fingerprint, not the rendered HTML -- that is the
    distinction the original comparison lost."""
    first = create_documentation_node(**ARGS)
    path = Path(first["path"])
    fingerprint = re.search(
        r'data-source-fingerprint="([^"]*)"', path.read_text(encoding="utf-8")
    ).group(1)

    time.sleep(1.1)
    create_documentation_node(**ARGS)

    assert re.search(
        r'data-source-fingerprint="([^"]*)"', path.read_text(encoding="utf-8")
    ).group(1) == fingerprint

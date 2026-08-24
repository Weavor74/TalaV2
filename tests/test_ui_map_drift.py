"""
Drift guard for the ui-view-* map (UI-011).

The ui-view-* nodes list element ids so Tala can answer "what can I do on this
screen", and a future ui.click tool would target them directly. That only holds
while the ids still exist in the renderer -- an id renamed in index.html would
otherwise leave the map confidently wrong, which is worse than having no map.

These tests fail loudly on that drift. Regenerate with:

    python scripts/generate_ui_nodes.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdk.agent.parser import parse_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "renderer" / "index.html"
NODES = ROOT / "nodes"

UI_NODES = sorted(NODES.glob("ui-view-*.html"))


def _real_ids() -> set[str]:
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', INDEX.read_text(encoding="utf-8")))


def _real_views() -> set[str]:
    html = INDEX.read_text(encoding="utf-8")
    return set(re.findall(r'id="(view-[a-z-]+)"', html))


def test_ui_nodes_exist() -> None:
    """The map should not silently disappear."""
    assert UI_NODES, "no ui-view-* nodes found; run scripts/generate_ui_nodes.py"
    assert (NODES / "ui-map-index.html").exists(), "ui-map-index.html is missing"


@pytest.mark.parametrize("node_path", UI_NODES, ids=lambda p: p.stem)
def test_every_documented_id_exists_in_the_dom(node_path: Path) -> None:
    """Every element id a ui-view-* node claims must be present in index.html."""
    real = _real_ids()
    node = parse_file(str(node_path))

    missing = [
        c["id"]
        for c in node.state_data.get("controls", [])
        if c.get("id", "").strip() and c["id"].strip() not in real
    ]

    assert not missing, (
        f"{node_path.name} documents element id(s) that no longer exist in "
        f"renderer/index.html: {missing}. Either the renderer changed and the map "
        f"needs regenerating, or the id was recorded wrong."
    )


@pytest.mark.parametrize("node_path", UI_NODES, ids=lambda p: p.stem)
def test_documented_view_id_exists(node_path: Path) -> None:
    """Each node's view_id must name a real <section> in the renderer."""
    views = _real_views()
    view_id = parse_file(str(node_path)).state_data.get("view_id", "")
    assert view_id in views, (
        f"{node_path.name} claims view_id {view_id!r}, which is not a section "
        f"in renderer/index.html. Known views: {sorted(views)}"
    )


def test_index_covers_every_view_node() -> None:
    """ui-map-index must point at every ui-view-* node, and only real ones."""
    index = parse_file(str(NODES / "ui-map-index.html"))
    listed = set(index.state_data.get("views", {}).values())
    on_disk = {p.stem for p in UI_NODES}

    assert listed == on_disk, (
        "ui-map-index is out of step with the ui-view-* nodes on disk. "
        f"Only in index: {sorted(listed - on_disk)}. "
        f"Only on disk: {sorted(on_disk - listed)}."
    )


def test_ids_are_literal_not_shorthand() -> None:
    """
    Ids must be single literal ids, never 'a / b' shorthand.

    A ui.click tool reads these directly, so a compound string would send it
    after an element that does not exist.
    """
    offenders = []
    for node_path in UI_NODES:
        for c in parse_file(str(node_path)).state_data.get("controls", []):
            cid = c.get("id", "")
            if any(ch in cid for ch in ("/", ",", "..")) or cid.strip() != cid:
                offenders.append(f"{node_path.name}: {cid!r}")

    assert not offenders, (
        "ui-view-* control ids must be single literal ids, not shorthand: " + "; ".join(offenders)
    )

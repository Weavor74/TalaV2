"""
Which node classes belong in a release artifact (P2 review item 23).

"Nodes ARE the data" is the design, so a packaged build shipping nodes/** is
intentional — but not every class of node is the product. Some are THIS
install's data:

  persona-*        identity. "No one else should have this TALA" — the same
                   requirement tests/test_persona_not_shareable.py enforces on
                   the peer firewall, applied to the release artifact.
  corpus-*         the voice-training corpus and its locally trained LoRA
                   weights (corpus-*_lora/ carries adapter_model.safetensors).
  learning-*       distilled from what THIS machine's sessions learned.
  benchmark-run-*  local benchmark output.
  flow-*           user-created flows, uuid-named.
  _staging/        nodes that have not passed promotion yet.

Two packagers must agree: electron-builder (package.json build.files) and
scripts/build_bundles.py (the VPS download zips). This test pins both, so a
new private node class gets excluded in one place per packager, knowingly.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO / "scripts"))
from build_bundles import BASE_EXCLUDES  # noqa: E402

PRIVATE_NODE_CLASSES = [
    "nodes/persona-*",
    "nodes/learning-*",
    "nodes/benchmark-run-*",
    "nodes/flow-*",
    "nodes/corpus-*",
]


def test_electron_builder_excludes_private_node_classes():
    files = json.loads((REPO / "package.json").read_text(encoding="utf-8"))["build"]["files"]
    for pattern in PRIVATE_NODE_CLASSES:
        assert f"!{pattern}" in files, (
            f"package.json build.files no longer excludes {pattern}; "
            "a packaged build would ship this install's private nodes"
        )
    assert "!nodes/_staging/**" in files


def test_vps_bundles_exclude_private_node_classes():
    excludes = BASE_EXCLUDES
    for pattern in PRIVATE_NODE_CLASSES:
        assert pattern in excludes, (
            f"scripts/build_bundles.py no longer excludes {pattern}; "
            "the public download zips would carry this install's private nodes"
        )
    assert "nodes/_staging/*" in excludes


def test_the_lora_weights_are_covered_by_the_corpus_pattern():
    """The trained adapter lives in a DIRECTORY (corpus-*_lora/), which glob
    semantics can silently miss. package.json needs the explicit /** variant."""
    files = json.loads((REPO / "package.json").read_text(encoding="utf-8"))["build"]["files"]
    assert "!nodes/corpus-*/**" in files

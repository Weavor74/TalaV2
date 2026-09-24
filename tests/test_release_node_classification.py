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
  settings-*       the user's live settings — settings_nn.py rewrites these on
                   every change and regenerates them from the database when
                   missing, so a fresh install loses nothing.
  _staging/        nodes that have not passed promotion yet.

There is ONE packager: scripts/build_bundles.py, which builds the zips that
upload_vps.ps1 publishes and the OTA updater applies. The electron-builder
format was dropped 2026-09-23 — its build.files allowlist had drifted from
BASE_EXCLUDES for months and its output was never published anywhere.
"""

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
    "nodes/settings-*",
]

# Live per-install state outside nodes/ naming: the running app writes these,
# the updater extracts a bundle over the install dir, so shipping them means
# every OTA update resets them to the build machine's copies.
LIVE_STATE_FILES = [
    "emotion_state.json",
    "skills/skill_*.yaml",
    "workflows/*-*-*-*-*.json",
]


def test_bundles_exclude_private_node_classes():
    excludes = BASE_EXCLUDES
    for pattern in PRIVATE_NODE_CLASSES:
        assert pattern in excludes, (
            f"scripts/build_bundles.py no longer excludes {pattern}; "
            "the public download zips would carry this install's private nodes"
        )
    assert "nodes/_staging/*" in excludes


def test_bundles_exclude_live_state_files():
    """An OTA update must never overwrite what the running app writes."""
    for pattern in LIVE_STATE_FILES:
        assert pattern in BASE_EXCLUDES, (
            f"scripts/build_bundles.py no longer excludes {pattern}; "
            "an OTA update would overwrite the user's live copy of it"
        )


def test_electron_builder_stays_gone():
    """The second distribution format was dropped, not forgotten (2026-09-23).

    Its files allowlist had to be maintained in lockstep with BASE_EXCLUDES,
    drifted anyway, and nothing it produced was ever published. If packaging
    with electron-builder comes back, it needs its own exclusion pinning
    equivalent to the tests above."""
    raw = (REPO / "package.json").read_text(encoding="utf-8")
    assert '"build"' not in raw and "electron-builder" not in raw, (
        "package.json carries electron-builder config again; either remove it "
        "or restore per-packager exclusion pinning in this test file"
    )

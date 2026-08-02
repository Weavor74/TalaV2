"""
One-off script: regenerate the `categories` block of
nodes/system-tala-rpc-surface-reference.html from the live capability
manifest (python/harness/capabilities.py), so the method-family lists
match _HANDLERS exactly instead of a hand-picked, drift-prone subset.

Only rewrites the machine-readable <script id="node-state"> JSON block.
The narrative sections (Repair Rules, Relationships, prose bullets) are
edited by hand separately -- this script is not meant to be wired into
CI or run automatically; re-run manually and review the diff whenever
the RPC surface changes enough to warrant it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from python.harness.capabilities import get_capability_manifest

# Maps each live method prefix to the named group it belongs to in the
# reference doc. Keep in sync by hand when new prefixes are added.
GROUPS: dict[str, str] = {
    "harness": "core_runtime", "system": "core_runtime", "command": "core_runtime",
    "db": "core_runtime", "token": "core_runtime", "graph": "core_runtime", "vault": "core_runtime",

    "inference": "inference",

    "auth": "configuration", "database": "configuration", "git": "configuration",
    "search": "configuration", "storage": "configuration", "runtime": "configuration",
    "mcp": "configuration", "gateway": "configuration",

    "guardrails": "builders", "workflow": "builders", "agent": "builders",
    "task": "builders", "swarm": "builders",

    "identity": "identity_trust", "profiles": "identity_trust", "contacts": "identity_trust",
    "p2p": "identity_trust", "chain": "identity_trust", "firewall": "identity_trust",

    "astro": "astro",

    "nodes": "memory_docs", "docs": "memory_docs", "wiki": "memory_docs",
    "memory": "memory_docs", "research": "memory_docs", "telemetry": "memory_docs",
    "logging": "memory_docs",

    "shell": "execution_recovery", "code": "execution_recovery", "test": "execution_recovery",
    "build": "execution_recovery", "repo": "execution_recovery", "plugins": "execution_recovery",
    "self_improvement": "execution_recovery",

    "browser": "device_capture", "multimodal": "device_capture",

    "heartbeat": "automation_marketplace", "marketplace": "automation_marketplace",
    "vps": "automation_marketplace",
}


def build_categories(manifest: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for prefix, methods in manifest["categories"].items():
        group = GROUPS.get(prefix)
        if group is None:
            ungrouped.append(prefix)
            continue
        grouped.setdefault(group, []).extend(methods)

    if ungrouped:
        raise SystemExit(
            f"regenerate_rpc_reference: no group mapping for prefixes: {ungrouped}. "
            "Add them to GROUPS before regenerating."
        )

    for methods in grouped.values():
        methods.sort()
    return {k: grouped[k] for k in sorted(grouped)}


def main() -> None:
    manifest = get_capability_manifest()
    categories = build_categories(manifest)

    doc_path = Path(__file__).resolve().parent.parent / "nodes" / "system-tala-rpc-surface-reference.html"
    text = doc_path.read_text(encoding="utf-8")

    # json.dumps nests two spaces per level by default; shift everything but
    # the opening "{" right by 4 spaces to match the surrounding <script>
    # block's existing 4-space body indent.
    lines = json.dumps(categories, indent=2).splitlines()
    new_categories_json = "\n".join(
        line if i == 0 else "    " + line
        for i, line in enumerate(lines)
    )

    pattern = re.compile(r'("categories":\s*)\{.*?\}(\s*\n\s*\})', re.DOTALL)
    replacement = r'\g<1>' + new_categories_json.replace("\\", "\\\\") + r'\g<2>'
    new_text, count = pattern.subn(replacement, text)
    if count != 1:
        raise SystemExit(f"expected exactly 1 categories block, found {count}")

    doc_path.write_text(new_text, encoding="utf-8")
    print(f"Rewrote categories block in {doc_path} ({sum(len(v) for v in categories.values())} methods, {len(categories)} groups)")


if __name__ == "__main__":
    main()

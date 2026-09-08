"""
classify_remaining_documents.py — the deliberate classification review.

Thirteen documents had no source reference after tool and program
documentation were wired up. The reviewer's instruction was not to attach
files to them until the number looked better, but to decide what each one
*is*:

    "Schema and authoring standards: identify the document as normative. Link
    the parser, validator and conformance tests as implementations accountable
    to that specification. A code change should trigger a conformance review,
    not automatically rewrite the standard."

    "Architecture and operational descriptions: reference the modules and
    configuration that implement their claims."

    "Conceptual documents: identify their scope and review owner explicitly.
    Don't attach arbitrary source files merely to improve coverage."

So this is a table, not an algorithm. Each entry is a judgement about one
document, with the reason recorded next to it, because that is the artifact —
a classification nobody can read the reasoning for is one nobody can
challenge later.

The direction of accountability is the part worth being careful about. For a
NORMATIVE document the code answers to the document: when `parser.py` changes,
the schema node has not gone stale, and rewriting it to match would let an
implementation bug redefine the rule it violated. What is owed is a
conformance review. For a DESCRIPTIVE document it runs the other way and the
document is what gets rebuilt.

    python scripts/classify_remaining_documents.py
    python scripts/classify_remaining_documents.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The project owner reviews the conceptual documents. Recorded rather than
#: left blank: "somebody should look at this" with no name attached is how a
#: document goes five months without anybody looking at it.
DEFAULT_REVIEW_OWNER = "steven"

CLASSIFICATION: tuple[dict, ...] = (
    # ── Normative: these define the rules, and code answers to them ─────────
    {
        "node_id": "system-neural-node-v2-schema",
        "accountability": "normative",
        "why": "It defines the node format. The parser and validator are "
               "implementations of this specification, not its source.",
        "conforming_implementations": [
            "sdk/agent/parser.py",
            "sdk/agent/validator.py",
            "tests/test_node_taxonomy.py",
        ],
    },
    {
        "node_id": "system-html-nn-authoring-guide",
        "accountability": "normative",
        "why": "The authoring standard. What conforms to it is the writer and "
               "the validator that admits what the writer produced.",
        "conforming_implementations": [
            "sdk/agent/documentation_nodes.py",
            "sdk/agent/validator.py",
        ],
    },
    {
        "node_id": "system-html-nn-activation-weights",
        "accountability": "normative",
        "why": "It states what weights ARE — static attributes, never learned "
               "and never updated by inference. The activation engine is "
               "answerable to that statement; if the engine started training "
               "them, the engine would be wrong, not this document.",
        "conforming_implementations": ["sdk/agent/activation.py"],
    },
    {
        "node_id": "system-self-improvement-loop",
        "accountability": "normative",
        "why": "It prescribes a discipline — observe, classify, choose the "
               "smallest safe artifact, verify — rather than describing what "
               "some module happens to do.",
        "conforming_implementations": [
            "sdk/agent/self_documenter.py",
            "sdk/agent/goal_engine.py",
        ],
    },

    # ── Descriptive: these answer to the code ───────────────────────────────
    {
        "node_id": "system-tala-agent-architecture",
        "accountability": "descriptive",
        "why": "It describes the shape the system currently has.",
        "source_files": [
            "python/harness/server.py",
            "sdk/agent/tools.py",
            "python/harness/db.py",
        ],
    },
    {
        "node_id": "system-tala-html-nn-memory-runtime",
        "accountability": "descriptive",
        "why": "It describes how nodes are stored, indexed and recalled.",
        "source_files": [
            "python/harness/memory.py",
            "python/harness/retrieval.py",
            "sdk/agent/documentation_nodes.py",
        ],
    },
    {
        "node_id": "system-tala-configurable-provider-surface",
        "accountability": "descriptive",
        "why": "It describes how providers are configured and probed.",
        "source_files": [
            "python/harness/config.py",
            "python/harness/search_manager.py",
        ],
    },
    {
        "node_id": "system-program-documentation-generator",
        "accountability": "descriptive",
        "why": "It describes what the generator does, and the generator is "
               "the authority on that.",
        "source_files": [
            "sdk/agent/documentation_nodes.py",
            "scripts/generate_sdk_agent_nodes.py",
        ],
    },

    # ── Conceptual: scope and owner, and deliberately no source files ───────
    {
        "node_id": "doc-self-improvement-discipline",
        "accountability": "conceptual",
        "why": "A discipline for approaching a change. No module implements a "
               "habit — and the one file it did name, "
               "skill-self-check-discipline.html, no longer exists, which is "
               "the dangling reference this clears rather than preserves.",
        "scope": "How to approach a self-improvement change: diagnose before "
                 "proposing, prefer the smallest safe artifact, verify from disk.",
    },
    {
        "node_id": "system-voice-adapter-training",
        "accountability": "descriptive",
        "why": "First read as conceptual — it argues that adapters shape voice "
               "and not knowledge. But it already named the whole training "
               "pipeline, and all of those files exist: it describes a path "
               "that is implemented, so it answers to that path.",
        "source_files": [
            "scripts/train_voice_adapter.py",
            "python/inference/lora_trainer.py",
            "python/inference/embedded_engine.py",
        ],
    },
    {
        "node_id": "system-ollama-adapter-deployment",
        "accountability": "descriptive",
        "why": "The constraint it records is external, but what it describes "
               "is the local adapter-selection path that runs into it, and it "
               "already named those files.",
        "source_files": [
            "python/harness/server.py",
            "python/inference/embedded_engine.py",
            "scripts/train_voice_adapter.py",
        ],
    },
    {
        "node_id": "widget_api_bridge_v1",
        "accountability": "conceptual",
        "why": "Integration patterns for third-party surfaces — a way of "
               "doing things rather than a description of one. It named "
               "widgets/external_api_stub.py, which no longer exists.",
        "scope": "Patterns for bridging external APIs into the widget surface.",
    },

    # ── Misfiled: a record wearing a description's prefix ───────────────────
    {
        "node_id": "sdk_agent_web_browser_skill",
        "purpose": "record",
        "accountability": "conceptual",
        "why": "Its own first line: 'Durable learning event captured by Tala.' "
               "It is a record of something learned, filed under a prefix the "
               "family rule reads as SDK module documentation — which is "
               "precisely why a declaration has to beat an inference. It "
               "names sdk/agent/web_browser.py, but a record of what was "
               "learned is not rewritten when that module changes.",
        "scope": "A learning event about controlling the internal browser.",
    },
)


def _apply(path: Path, entry: dict) -> str:
    """Stamp one node's classification. Returns what happened."""
    from sdk.agent.documentation_nodes import accountability_of, purpose_of

    html = path.read_text(encoding="utf-8", errors="replace")
    node_id = entry["node_id"]
    accountability = accountability_of(entry.get("accountability", ""))
    purpose = purpose_of(node_id, entry.get("purpose", ""))
    conforming = entry.get("conforming_implementations") or []
    sources = entry.get("source_files") or []
    owner = entry.get("review_owner") or (
        DEFAULT_REVIEW_OWNER if accountability == "conceptual" else ""
    )

    missing = [f for f in (conforming + sources) if not (ROOT / f).exists()]
    if missing:
        return f"REFUSED: names files that do not exist: {missing}"

    from sdk.agent.documentation_nodes import _source_digest

    digest = _source_digest(conforming or sources) if (conforming or sources) else ""

    replacements = {
        "data-document-purpose": purpose,
        "data-document-accountability": accountability,
        "data-conforming-implementations": ";".join(conforming),
        "data-review-owner": owner,
    }
    if sources:
        replacements["data-source-files"] = ";".join(sources)
    elif accountability == "conceptual":
        # Clear whatever it named. Two of these pointed at files that no
        # longer exist, and a conceptual document holding a dangling
        # reference is worse than one holding none: it looks accountable to
        # something, and the something is gone.
        replacements["data-source-files"] = ""
        replacements["data-source-digest"] = ""
    if digest:
        replacements["data-source-digest"] = digest

    for name, value in replacements.items():
        if re.search(rf'{name}="[^"]*"', html):
            html = re.sub(rf'{name}="[^"]*"', f'{name}="{value}"', html, count=1)
        else:
            html = re.sub(
                r'(data-schema-version="[^"]*")',
                lambda m: f'{m.group(1)}\n  {name}="{value}"', html, count=1,
            )

    def _stamp(match: re.Match) -> str:
        try:
            state = json.loads(match.group(1))
        except Exception:
            return match.group(0)
        state.setdefault("metadata", {})
        state["metadata"].update({
            "document_purpose": purpose,
            "document_accountability": accountability,
            "conforming_implementations": conforming,
            "scope": entry.get("scope", ""),
            "review_owner": owner,
            "classification_reason": entry["why"],
        })
        if sources:
            state["metadata"]["source_files"] = sources
            state["metadata"]["source_digest"] = digest
        return match.group(0).replace(
            match.group(1), json.dumps(state, indent=2, ensure_ascii=False)
        )

    html = re.sub(
        r'<script type="application/json"[^>]*>(.*?)</script>',
        _stamp, html, count=1, flags=re.DOTALL,
    )
    path.write_text(html, encoding="utf-8")
    return f"{accountability}"


def main(check_only: bool) -> int:
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir

    nodes_dir = resolve_active_nodes_dir()
    problems = 0
    for entry in CLASSIFICATION:
        path = nodes_dir / f"{entry['node_id']}.html"
        if not path.exists():
            print(f"MISSING   {entry['node_id']}")
            problems += 1
            continue
        if check_only:
            html = path.read_text(encoding="utf-8", errors="replace")
            declared = re.search(r'data-document-accountability="([^"]+)"', html)
            state = "ok" if declared else "UNCLASSIFIED"
            if not declared:
                problems += 1
            print(f"{state:12} {entry['node_id']}")
            continue
        outcome = _apply(path, entry)
        if outcome.startswith("REFUSED"):
            problems += 1
        print(f"{outcome:12} {entry['node_id']}  — {entry['why'][:70]}")

    print(f"\n{len(CLASSIFICATION)} document(s), {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report which of these are unclassified, change nothing")
    raise SystemExit(main(ap.parse_args().check))

"""
scripts/prune_junk_learning_nodes.py
====================================
Apply the learning-quality gate retroactively to learning nodes already on disk.

The gate in sdk/agent/documentation_nodes.py stops new junk from being written.
This removes what accumulated before it existed -- task restatements from
benchmark runs, completion boilerplate, and fragments -- so the RAG-graph's
wiki contains only pages worth retrieving.

Dry run by default. Pass --apply to actually delete, which also clears the
node's rows from the Postgres memory_index so retrieval stops returning it.

    python scripts/prune_junk_learning_nodes.py
    python scripts/prune_junk_learning_nodes.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sdk.agent.documentation_nodes import assess_learning_quality  # noqa: E402

NODES = REPO_ROOT / "nodes"
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.I)
SEMANTIC_RE = re.compile(
    r'<section id="semantic-memory".*?</section>', re.DOTALL | re.I
)


def strip_tags(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s).split())


def node_topic_and_summary(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    m = TITLE_RE.search(raw)
    title = strip_tags(m.group(1)) if m else ""
    topic = title
    for prefix in ("Learning:", "Skill:"):
        if topic.startswith(prefix):
            topic = topic[len(prefix):].strip()
            break

    s = SEMANTIC_RE.search(raw)
    body = strip_tags(s.group(0)) if s else ""
    # Drop the fixed preamble so the gate sees the actual lesson.
    body = body.replace("Durable learning event captured by Tala.", "")
    body = re.sub(r"^\s*Topic:.*?\.\s*", "", body).strip()
    return topic, body


async def clear_index_rows(node_ids: list[str]) -> int:
    from python.harness import db

    await db.init_pool()
    total = 0
    for nid in node_ids:
        res = await db.execute("DELETE FROM memory_index WHERE ref_id = $1", nid)
        tail = str(res).split()[-1]
        total += int(tail) if tail.isdigit() else 0
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument(
        "--include-skills",
        action="store_true",
        help="also prune junk skill-* nodes",
    )
    args = ap.parse_args()

    # Skill nodes are the same junk family as learning nodes and arguably worse.
    # self_documenter writes one per completed goal, titled with the raw goal
    # text, and unlike a learning node its *body is raw Python*. Those bodies
    # get indexed and retrieved: asked to run a self-test, Tala retrieved
    # "Skill: Create a new reusable skill by writing a Python file at
    # sdk/agent/skills/clean_broken_skills.py" and regurgitated that file's
    # source instead of calling any tool. Code-bearing junk in the wiki does
    # not just waste space -- it hijacks the response.
    patterns = ["learning-*.html"]
    if args.include_skills:
        patterns.append("skill-*.html")

    files = sorted(f for p in patterns for f in NODES.glob(p))
    if not files:
        print("no learning nodes found")
        return 0

    keep, drop = [], []
    for f in files:
        topic, summary = node_topic_and_summary(f)
        reason = assess_learning_quality(topic, summary)
        (drop if reason else keep).append((f, topic, reason))

    print(f"{len(files)} learning nodes\n")
    print("KEEP")
    for f, topic, _ in keep:
        print(f"  {topic[:78]}")
    print(f"\nPRUNE ({len(drop)})")
    for f, topic, reason in drop:
        label = topic[:60] if topic else "(blank title)"
        print(f"  {label}")
        print(f"      {reason}")

    if not args.apply:
        print(f"\nDRY RUN -- nothing deleted. Re-run with --apply to remove {len(drop)}.")
        return 0

    node_ids = [f.stem for f, _, _ in drop]
    for f, _, _ in drop:
        f.unlink()
    print(f"\ndeleted {len(drop)} node files")

    try:
        n = asyncio.run(clear_index_rows(node_ids))
        print(f"cleared {n} memory_index row(s)")
    except Exception as exc:
        print(f"could not clear index rows ({exc}); run a reindex to reconcile")

    print(f"{len(keep)} learning nodes remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

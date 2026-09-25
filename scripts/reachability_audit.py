"""
scripts/reachability_audit.py — find code that nothing reaches.

WHY THIS EXISTS
---------------
This codebase's dominant defect is not broken code. It is code that is
correct, imported, tested, and never called. Nine confirmed instances so far,
each of which looked healthy in isolation while doing nothing in production,
and each found by hand months after it shipped:

  1. the astro-emotion MCP that never existed
  2. the chat_mode gate reading a key nothing wrote
  3. the DAG executor with no tool node type
  4. emotion that never entered the model's messages
  5. AdapterPacket governance passed in and never read
  6. browser.execute_js broadcasting to no listener
  7. the Research panel's error banner with no feed
  8. the emotion valence projection, which a node claimed had readers
  9. the architectural invariants, which nothing ever ran

Unit tests never catch this class, because they test the component and the
component works. What is missing is a caller, and only a whole-tree view can
see that.

HOW IT WORKS
------------
Every public function and method is traced to a caller outside its own module.
Reached by any of these routes counts as alive:

  * a call from another module under sdk/ or python/
  * a reference from the renderer, electron, scripts/, or a workflow
  * registration via a decorator (@register_tool, @property, @fixture, ...)
  * server.py's handle_* functions, dispatched through _HANDLERS
  * a method on a class that is used elsewhere

Two things are deliberately NOT evidence of life:

  * a mention in nodes/*.html. The self-documenting wiki names every function
    in the tree, so accepting doc mentions marks everything reachable and the
    audit reports nothing. This is how the first version of this script
    produced a clean bill of health for a tree with 180 unreached entry
    points.
  * a call from a test. A function whose only caller is its own test is the
    exact shape being hunted; it is reported as TEST-ONLY rather than hidden.

USAGE
-----
    python scripts/reachability_audit.py              # report
    python scripts/reachability_audit.py --baseline   # rewrite the ratchet file

tests/test_no_new_dead_subsystems.py fails when an entry point appears that is
not in the baseline. The baseline shrinks as things are wired or deleted; it
must never grow without a reason stated in the commit.
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "reachability_baseline.json"

CODE_ROOTS = ("sdk", "python")
SKIP_PARTS = ("tests", "__pycache__", ".venv", "node_modules")
SERVER = "python/harness/server.py"

#: Decorators that hand a function to a registry or the language itself.
DECOR_REGISTERED = frozenset({
    "register_tool", "tool", "register", "property", "staticmethod",
    "classmethod", "setter", "fixture", "abstractmethod", "cached_property",
    "overload", "route", "on", "app", "event",
    "model_validator", "field_validator", "validator", "root_validator",
})


def _norm(p: pathlib.Path) -> str:
    return str(p).replace("\\", "/")


def _rel(p: pathlib.Path) -> str:
    return _norm(p.relative_to(ROOT))


def _decorators(node: ast.AST) -> list[str]:
    out: list[str] = []
    for dec in getattr(node, "decorator_list", []):
        cur = dec.func if isinstance(dec, ast.Call) else dec
        while isinstance(cur, ast.Attribute):
            out.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            out.append(cur.id)
    return out


def collect() -> list[dict]:
    code: dict[str, str] = {}
    for root in CODE_ROOTS:
        for path in (ROOT / root).rglob("*.py"):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            code[_rel(path)] = path.read_text(encoding="utf-8", errors="replace")

    # Other surfaces a call can come from. nodes/*.html is excluded on purpose.
    other = ""
    for pattern in ("renderer/*.js", "renderer/*.html", "electron/*.js",
                    "scripts/*.py", "workflows/*.json"):
        for path in ROOT.glob(pattern):
            if _rel(path) == "scripts/reachability_audit.py":
                continue
            other += path.read_text(encoding="utf-8", errors="replace")

    tests = ""
    for pattern in ("tests/**/*.py", "sdk/**/tests/*.py"):
        for path in ROOT.glob(pattern):
            tests += path.read_text(encoding="utf-8", errors="replace")

    candidates = []
    for rel, src in code.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") or node.name == "main":
                continue
            if any(d in DECOR_REGISTERED for d in _decorators(node)):
                continue
            if rel == SERVER and node.name.startswith("handle_"):
                continue
            owner = parents.get(node)
            owner = owner.name if isinstance(owner, ast.ClassDef) else None
            candidates.append((rel, node.name, node.lineno, owner))

    findings = []
    for rel, name, line, owner in candidates:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        if any(pat.search(text) for other_rel, text in code.items() if other_rel != rel):
            continue
        if pat.search(other):
            continue
        if owner:
            cls_pat = re.compile(r"\b" + re.escape(owner) + r"\b")
            used_elsewhere = any(
                cls_pat.search(t) for r, t in code.items() if r != rel
            ) or cls_pat.search(other)
            # A method on a class used elsewhere is only suspicious if the
            # method name appears nowhere but its own definition.
            if used_elsewhere and len(pat.findall(code[rel])) > 1:
                continue
        findings.append({
            "module": rel,
            "name": f"{owner}.{name}" if owner else name,
            "line": line,
            "test_only": bool(pat.search(tests)),
        })

    findings.sort(key=lambda f: (f["module"], f["line"]))
    return findings


def key(finding: dict) -> str:
    return f"{finding['module']}::{finding['name']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="store_true",
                    help="rewrite the ratchet file from the current tree")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    findings = collect()

    if args.baseline:
        BASELINE.write_text(
            json.dumps(sorted(key(f) for f in findings), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(findings)} entries to {_rel(BASELINE)}")
        return 0

    if args.json:
        print(json.dumps(findings, indent=2))
        return 0

    by_module = collections.defaultdict(list)
    for f in findings:
        by_module[f["module"]].append(f)

    print(f"UNREACHED PUBLIC ENTRY POINTS: {len(findings)} "
          f"across {len(by_module)} module(s)")
    print()
    for module in sorted(by_module, key=lambda m: -len(by_module[m])):
        items = by_module[module]
        print(f"{module}  ({len(items)})")
        for f in items:
            tag = "TEST-ONLY" if f["test_only"] else "NO CALLER"
            print(f"    {f['name']:<46} :{f['line']:<6} {tag}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

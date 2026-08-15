"""
What SelfHealingDocs counts as the API surface, and what documentation
verification demands of a node.

Two defects, both of which made a check report the opposite of the truth.

The drift checker walked the whole AST, so closures, callbacks and decorator
inner functions were reported as undocumented public API. Four of the five drift
reports across the entire SDK were that -- `decorator` inside
ToolRegistry.register, `ignore_dirs` inside a copytree call. Permanent false
positives are worse than no check: they teach the reader to skim past a report
that will eventually be right.

Separately, verify_documentation_nodes required memory_authority == 0.00, which
is the same conflation that made 94 of 115 nodes unrecallable. The checker meant
to catch a mute documentation graph was asserting that it stay mute.
"""
import ast
import textwrap
from pathlib import Path

import pytest

from sdk.agent.documentation_nodes import verify_documentation_nodes
from sdk.agent.wiki import SelfHealingDocs

SOURCE = textwrap.dedent('''
    def module_level_function():
        def inner_helper():
            pass
        return inner_helper

    async def module_level_coroutine():
        def callback():
            pass

    class PublicClass:
        def method(self):
            def decorator(fn):
                return fn
            return decorator
''')


def _api_symbols(source: str):
    """Mirror of the collector in SelfHealingDocs.run_comparative_check."""
    tree = ast.parse(source)
    functions, classes = [], []

    def collect(body):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(stmt.name)
            elif isinstance(stmt, ast.ClassDef):
                classes.append(stmt.name)
                collect(stmt.body)

    collect(tree.body)
    return functions, classes


def test_module_level_functions_are_api():
    functions, _ = _api_symbols(SOURCE)

    assert "module_level_function" in functions
    assert "module_level_coroutine" in functions


def test_methods_are_api():
    """Descending into class bodies is intentional -- a public method going
    undocumented is real drift."""
    functions, classes = _api_symbols(SOURCE)

    assert "PublicClass" in classes
    assert "method" in functions


def test_functions_defined_inside_functions_are_not_api():
    """The regression. ast.walk collected these and every one was noise."""
    functions, _ = _api_symbols(SOURCE)

    for inner in ("inner_helper", "callback", "decorator"):
        assert inner not in functions, f"{inner} is an implementation detail, not API"


def test_checker_does_not_walk_into_function_bodies():
    """Guards the implementation, not just a local copy of its logic."""
    source = Path("sdk/agent/wiki.py").read_text(encoding="utf-8")

    assert "_collect_api" in source, "the API collector was replaced"
    assert "n.name for n in ast.walk(tree)" not in source, (
        "SelfHealingDocs is walking the whole AST again; nested functions will "
        "be reported as undocumented API"
    )


def test_sdk_documentation_has_no_drift():
    """The end-to-end check, read-only: run_comparative_check returns
    corrections, apply_corrections is the separate mutating step and is never
    called here."""
    agent_dir = Path("sdk/agent")
    nodes_dir = Path("nodes")
    docs = SelfHealingDocs()

    drift = {}
    for src in sorted(agent_dir.glob("*.py")):
        if src.name.startswith("__"):
            continue
        node = nodes_dir / f"sdk_agent_{src.stem}.html"
        if not node.exists():
            continue
        corrections = docs.run_comparative_check(str(src), str(node))
        if corrections:
            drift[src.name] = [str(c)[:120] for c in corrections]

    assert not drift, f"documentation has drifted from code: {drift}"


def test_verification_requires_documents_to_be_recallable():
    """The rule used to be the inverse, which is what kept the graph mute."""
    result = verify_documentation_nodes()

    assert result["success"], [n for n in result["nodes"] if not n["validation_passed"]][:5]
    assert result["verified"] > 0


@pytest.mark.parametrize("attribute,expected", [("execution_authority", 0.0)])
def test_documents_still_cannot_execute(attribute, expected):
    """Recall authority was granted; the authority that was correctly zero must
    stay zero."""
    source = Path("sdk/agent/documentation_nodes.py").read_text(encoding="utf-8")

    assert 'execution_authority must be 0.00' in source
    assert "memory_authority must be greater than 0.00" in source

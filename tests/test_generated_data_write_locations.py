"""
Generated data must not be written relative to the code.

Three separate instances of this bug turned up in one session:

* ``SelfDocumenter`` wrote skill nodes relative to ``WorkflowExecutor.root_dir``,
  which defaults to ``Path(__file__).parents[2]`` -- the checkout. Every
  ``pytest`` rewrote ``nodes/skill-workflow_test.html`` in the repository.
* ``GoalTreeDecomposer`` wrote ``tot_shortlist.json`` with a bare relative
  path, so it landed in whatever directory the process was started from.
* The benchmark runner's readback bound a frozen ``settings`` singleton and
  queried the wrong database entirely.

They share a shape: data whose home is the user's portable data root, resolved
instead against the code or the current directory. On a build meant to run from
a USB stick that is not untidiness, it is data written to the wrong machine.

``resolve_user_data_dir()`` is the single canonical resolver and its own
docstring says every node/workflow-writing path should go through it. This test
is the enforcement, because the previous three were all found by accident.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Writing a bare filename lands the file wherever the process happens to be.
_BARE_WRITE_RE = re.compile(
    r"""(?:open|write_text|write_bytes)\(\s*["'][A-Za-z_][\w\-]*\.(?:json|txt|md|log|html|csv|ya?ml)["']""",
)

_SEARCHED = ("sdk", "python", "scripts")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for area in _SEARCHED:
        for path in (REPO_ROOT / area).rglob("*.py"):
            if "__pycache__" in path.parts or path.name.startswith("test_"):
                continue
            files.append(path)
    return files


def test_no_module_writes_generated_data_to_a_bare_relative_path():
    offenders: list[str] = []
    for path in _source_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for number, line in enumerate(source.splitlines(), start=1):
            if _BARE_WRITE_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:90]}")

    assert not offenders, (
        "these write generated data to the process working directory rather than "
        "the resolved user-data root:\n  " + "\n  ".join(offenders)
    )


def test_the_tot_shortlist_goes_to_the_data_root():
    """The specific regression: it used to be open("tot_shortlist.json", "w")."""
    source = (REPO_ROOT / "sdk" / "agent" / "queue.py").read_text(encoding="utf-8")

    assert 'open("tot_shortlist.json"' not in source
    assert "resolve_user_data_dir() / \"tot_shortlist.json\"" in source


@pytest.mark.parametrize("area", _SEARCHED)
def test_the_scan_actually_reads_files(area):
    """
    A guard that silently matched nothing would pass forever. This asserts the
    search has real files to look at, so a broken path cannot make the test
    above vacuous.
    """
    found = [p for p in _source_files() if p.is_relative_to(REPO_ROOT / area)]
    assert found, f"no python sources found under {area}/ -- the scan is not looking anywhere"

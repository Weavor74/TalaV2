"""
Root pytest configuration.

Exists for one reason: the suite is tiered (see `[tool.pytest.ini_options]` in
pyproject.toml), and a tier that is skipped quietly is worse than a tier that is
slow. The default `pytest` run deselects the `llm`-marked tests -- the ones that
drive a real model or the running harness -- because those 11 tests held
essentially the entire 41-minute runtime of the old full suite.

Deselecting them silently would be the same shape as the bugs this branch was
opened to remove: a path that turns "did not happen" into "looks fine". So every
run ends with an explicit count of what did not run and the command to run it.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Stash the deselected count where the summary hook can reach it. pytest
    # reports deselection through the hook below rather than on the config.
    config._llm_deselected = 0  # type: ignore[attr-defined]


def pytest_deselected(items: list[pytest.Item]) -> None:
    if not items:
        return
    config = items[0].config
    current = getattr(config, "_llm_deselected", 0)
    config._llm_deselected = current + len(items)  # type: ignore[attr-defined]


def pytest_terminal_summary(
    terminalreporter, exitstatus, config: pytest.Config
) -> None:
    """Say plainly what was not run."""
    deselected = getattr(config, "_llm_deselected", 0)
    if not deselected:
        return

    # Only shout when the deselection came from the default tiering. If the
    # caller passed their own -m expression they already know what they asked
    # for, and a nag would just be noise.
    markexpr = config.getoption("-m", default="")
    if markexpr != "not llm":
        return

    terminalreporter.write_sep("=", "NOT RUN", yellow=True, bold=True)
    terminalreporter.write_line(
        f"{deselected} test(s) marked `llm` were deselected. They drive a real "
        "model or the running harness and take minutes each."
    )
    terminalreporter.write_line("  pytest -m llm    run only those")
    terminalreporter.write_line("  pytest -m \"\"     run everything")

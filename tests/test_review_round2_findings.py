"""
Second cloud review, on the slice that was almost entirely my own work.

Three findings, all real, all the same failure class the branch exists to close:

  cron editor    opening a schedule the builder could not represent silently
                 rewrote it before the user saw it -- including the broken row
                 the UI labels "press Edit to fix".

  lora trainer   the peft-missing branch wrote a placeholder to the real
                 .safetensors path, and the JIT selector gates on Path.exists(),
                 so training could never be queued for that node again.

  db isolation   the guard against writing to the production database had three
                 paths that silently did nothing, plus a promised warning that
                 was never implemented.
"""
import os
import re
from pathlib import Path

import pytest

import conftest

RENDERER = Path("renderer/renderer.js")
TRAINER = Path("python/inference/lora_trainer.py")


# --- cron editor: never silently rewrite what it was given -------------------


def _detect_and_rebuild(expr: str) -> tuple[str, str]:
    """Mirror of detectCronMode + buildCronExpression for the `minutes` mode."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return "advanced", expr
    minute, hour, dom, month, dow = parts
    if month != "*":
        return "advanced", expr
    if minute.startswith("*/") and dom == "*" and dow == "*":
        window = re.fullmatch(r"(\d+)-23,0-(\d+)", hour) or re.fullmatch(r"(\d+)-(\d+)", hour)
        return "minutes", f"*/{minute[2:]} {hour if window else '*'} * * *"
    return "advanced", expr


@pytest.mark.parametrize("expr", [
    "*/30 22-23,8-6 * * *",   # the broken one the row invites you to fix
    "*/30 1-3,5-7 * * *",     # valid, two ranges the builder has no control for
    "*/15 9,13,17 * * *",     # valid, a discrete hour list
])
def test_builder_cannot_represent_these_so_the_editor_must_not_claim_it_can(expr):
    """Documents the shape rather than asserting it away. detectCronMode only
    inspects the minute/dom/dow fields, so it returns `minutes` for hour fields
    the builder has no control for -- and syncCronBuilder then overwrites the raw
    input with what the controls say."""
    mode, rebuilt = _detect_and_rebuild(expr)

    assert mode == "minutes"
    assert rebuilt != expr, "if this now round-trips, the guard below is untested"


def test_the_editor_falls_back_to_advanced_when_it_cannot_round_trip():
    """The fix: compare the round trip and drop to Advanced when it differs, so
    the original stays visible and editable instead of being replaced."""
    source = RENDERER.read_text(encoding="utf-8")
    block = source.split("loadCronIntoBuilder(expr)", 1)[1].split("openScheduleEditor(", 1)[0]

    assert "this._cronInput.value.trim() !== original" in block, (
        "loadCronIntoBuilder must detect that it failed to reproduce the expression"
    )
    assert "'advanced'" in block
    assert "this._cronInput.value = original" in block, "the original must be restored"


def test_a_broken_schedule_still_gets_a_change_warning():
    """The warning was suppressed for jobs with a parse_error, which removed the
    only safety net from the exact row the UI tells you to repair."""
    source = RENDERER.read_text(encoding="utf-8")
    opener = source.split("openScheduleEditor(id = null)", 1)[1].split("closeScheduleEditor()", 1)[0]

    assert "job ? job.cron_expr : null" in opener
    assert "!job.parse_error" not in opener, (
        "unparseable jobs are excluded from the change warning again"
    )


def test_a_representable_schedule_still_opens_in_its_builder_mode():
    """The fallback must not swallow the feature: the nightly job's overnight
    window round-trips exactly and should still open in `minutes`."""
    mode, rebuilt = _detect_and_rebuild("*/30 22-23,0-6 * * *")

    assert mode == "minutes"
    assert rebuilt == "*/30 22-23,0-6 * * *"


# --- lora trainer: no placeholder on the adapter path -----------------------


def test_the_peft_missing_branch_writes_nothing():
    """The JIT selector gates on Path.exists(), so a placeholder at the adapter
    path permanently prevents that node from ever queuing training again -- even
    once peft is installed. It also rebuilt the files commit 16e93a5 deleted."""
    source = TRAINER.read_text(encoding="utf-8")
    branch = source.split("if not is_peft_available():", 1)[1].split("logger.info(", 1)[0]
    # Comments in this branch describe the removed behaviour by name, so assert
    # against code only.
    code = "\n".join(
        line for line in branch.splitlines() if not line.strip().startswith("#")
    )

    assert "DUMMY_LORA_WEIGHTS_FOR" not in code, "a placeholder is written again"
    assert "open(output_path" not in code and "write_text" not in code
    assert "mkdir(" not in code, "the adapter directory must not be created either"
    assert "raise LoraTrainingUnavailable" in code


def test_no_placeholder_adapters_remain_in_the_repository():
    placeholders = [
        p for p in Path("nodes").glob("*_lora/adapter_model.safetensors")
        if p.stat().st_size < 4096
    ]

    assert not placeholders, f"placeholder adapters are back: {placeholders}"


# --- db isolation: no silent no-op paths ------------------------------------


def test_the_dotenv_parse_matches_the_application():
    """A hand-rolled parser missed `export TALA_DB_URL=...` and ignored
    TALA_ENV_PATH, which config.py honours. Either divergence yields an empty
    string, which _isolate_database treats as "nothing configured"."""
    source = Path("conftest.py").read_text(encoding="utf-8")

    assert "dotenv_values" in source, "the .env must be parsed the way the app parses it"
    assert "TALA_ENV_PATH" in source, "config.py honours TALA_ENV_PATH; this must too"


def test_the_connection_query_string_survives():
    """sslmode and friends live in the query string. Dropping it broke every
    managed Postgres that requires TLS, at import time, silently."""
    source = Path("conftest.py").read_text(encoding="utf-8")

    assert 'database.partition("?")' in source
    assert "_test{suffix}" in source


def test_running_against_production_is_actually_reported():
    """The flag was set in two branches and read in none, so the docstring's
    promise that the summary 'says so loudly' was unimplemented."""
    source = Path("conftest.py").read_text(encoding="utf-8")

    assert "_PRODUCTION_DB_REASON" in source
    summary = source.split("def pytest_terminal_summary", 1)[1]
    assert "_PRODUCTION_DB_REASON" in summary, "the reason is never surfaced"
    # Must not sit behind the deselection early-return.
    assert summary.index("_PRODUCTION_DB_REASON") < summary.index("if not deselected")


def test_this_run_is_isolated():
    if not conftest._PRODUCTION_DB_URL or os.environ.get("TALA_ALLOW_PRODUCTION_DB") == "1":
        pytest.skip("nothing configured, or production explicitly opted into")

    assert conftest._PRODUCTION_DB_REASON is None
    assert os.environ.get("TALA_DB_URL") != conftest._PRODUCTION_DB_URL

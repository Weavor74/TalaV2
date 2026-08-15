"""
Heartbeat run status, and the Mission Control panel that displays it.

last_status is written 'running' before a goal starts and 'success'/'failed'
after it returns. Nothing writes it if the process never gets there -- a restart,
or a BaseException such as CancelledError on shutdown or the SystemExit the
corpus pipeline raises by design, neither of which the executor's
`except Exception` catches.

Found live: the nightly corpus job read 'running' sixteen hours after its 02:00
start. Nothing was blocked -- is_due() ignores status -- so nothing failed. It
simply reported work that was not happening, which is worse than reporting a
failure, and any UI built on the field inherits that.
"""
import re
from pathlib import Path

SCHEDULER = Path("python/harness/heartbeat_scheduler.py")
RENDERER = Path("renderer/renderer.js")
INDEX = Path("renderer/index.html")


# --- status can no longer be stranded ---------------------------------------


def test_startup_settles_runs_orphaned_by_a_previous_process():
    source = SCHEDULER.read_text(encoding="utf-8")

    assert "_reconcile_orphaned_runs" in source
    assert "WHERE last_status = 'running'" in source
    # Reconcile must run before the tick loop can start new work, or a job that
    # fires immediately races the cleanup and gets settled mid-run.
    assert source.index("await self._reconcile_orphaned_runs()") < source.index("self._running = True")


def test_base_exceptions_still_record_a_terminal_status():
    """CancelledError and SystemExit are BaseException. Without this clause the
    status row is never written and stays 'running' forever."""
    source = SCHEDULER.read_text(encoding="utf-8")

    assert "except BaseException as exc:" in source
    # Slice to the end of the clause rather than a fixed character count, which
    # the explanatory comment overran.
    body = source.split("except BaseException as exc:", 1)[1].split("# Update in-memory state", 1)[0]
    assert "_persist_run_result" in body, "interrupted runs must still persist a status"
    assert re.search(r"^\s+raise\s*$", body, re.M), "the interruption must be re-raised, not swallowed"


def test_interrupted_is_distinct_from_failed():
    """An interrupted run may have completed its work before the process died.
    Recording a failure nobody observed is the same mistake in reverse."""
    source = SCHEDULER.read_text(encoding="utf-8")

    assert "'interrupted'" in source or '"interrupted"' in source


# --- the UI exists and is wired --------------------------------------------


def test_mission_control_has_schedule_controls():
    """The scheduler has run from startup since it was written, and no UI ever
    called any of its six RPC methods."""
    markup = INDEX.read_text(encoding="utf-8")

    for element in (
        'id="mc-schedule-list"',
        'id="mc-schedule-editor"',
        'id="btn-mc-schedule-new"',
        'id="mc-schedule-name"',
        'id="mc-schedule-cron"',
        'id="mc-schedule-goal"',
    ):
        assert element in markup, f"Mission Control is missing {element}"


def test_every_schedule_rpc_the_ui_needs_is_called():
    source = RENDERER.read_text(encoding="utf-8")

    for method in ("heartbeat.list", "heartbeat.create", "heartbeat.update",
                   "heartbeat.delete", "heartbeat.fire_now"):
        assert f"'{method}'" in source, f"the panel never calls {method}"


def test_schedule_rows_escape_user_supplied_text():
    """Names and goals are free text and are rendered into innerHTML."""
    source = RENDERER.read_text(encoding="utf-8")
    block = source.split("async loadSchedules()", 1)[1].split("openScheduleEditor(", 1)[0]

    # Asserts on every interpolation of a job field rather than on one call
    # spelling, so `job.goal_text || ''` counts and a future raw `${job.name}`
    # does not slip through.
    for interpolation in re.findall(r"\$\{([^}]*job\.[a-z_]+[^}]*)\}", block):
        if "escapeHtml" in interpolation or "job.enabled" in interpolation:
            continue
        raise AssertionError(f"job field interpolated without escaping: ${{{interpolation}}}")


def test_save_surfaces_the_harness_error_instead_of_swallowing_it():
    """An invalid cron expression must be reported. Accepted-and-never-fired is
    the failure this whole area is prone to."""
    source = RENDERER.read_text(encoding="utf-8")
    block = source.split("async saveSchedule()", 1)[1].split("async toggleSchedule(", 1)[0]

    assert "_scheduleError" in block
    assert "catch" in block


def test_descending_range_is_rejected_rather_than_silently_empty():
    """'8-6' is empty in every cron implementation. Accepted, it produces a
    schedule that fires on a subset of what was written -- observed live, where
    '*/30 22-23,8-6 * * *' resolved to hours [22, 23] alone."""
    from python.harness.heartbeat_scheduler import CronExpression

    import pytest as _pytest
    with _pytest.raises(ValueError, match="do not wrap around"):
        CronExpression("*/30 22-23,8-6 * * *")


def test_an_unparseable_job_is_kept_and_flagged_not_dropped():
    """heartbeat.list reads the scheduler's in-memory jobs, and reload_from_db
    skips anything that raises on construction -- so a stricter parser would
    make an existing schedule vanish from the UI, which is worse than the bad
    expression it rejected."""
    from datetime import UTC, datetime

    from python.harness.heartbeat_scheduler import HeartbeatJob

    job = HeartbeatJob(id="x", name="Test001", cron_expr="*/30 22-23,8-6 * * *", goal_text="Say hi")

    assert job.parse_error, "the job must carry its error for the UI to show"
    assert job.is_valid is False
    assert not any(
        job.is_due(datetime(2026, 8, 15, h, m, tzinfo=UTC))
        for h in range(24) for m in (0, 30)
    ), "a job that cannot be parsed must never fire"


def test_parse_error_reaches_the_ui():
    scheduler = SCHEDULER.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")

    assert '"parse_error": j.parse_error' in scheduler, "get_jobs must expose the error"
    assert "job.parse_error" in renderer, "the panel must show a job that can never fire"


def test_builder_covers_every_mode_it_offers():
    """A mode in the dropdown with no branch in buildCronExpression would fall
    through to the raw input and silently produce the previous mode's value."""
    markup = INDEX.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")

    block = markup.split('id="mc-cron-mode"', 1)[1].split("</select>", 1)[0]
    offered = set(re.findall(r'value="([a-z]+)"', block))
    builder = renderer.split("buildCronExpression()", 1)[1].split("describeCron(", 1)[0]

    for mode in offered - {"advanced"}:
        assert f"case '{mode}'" in builder, f"mode '{mode}' is offered but never built"


def test_overnight_window_is_emitted_as_two_ranges():
    """The whole point: an hour window crossing midnight has no wrap form in
    cron and must become 'from-23,0-to'."""
    renderer = RENDERER.read_text(encoding="utf-8")
    builder = renderer.split("buildCronExpression()", 1)[1].split("describeCron(", 1)[0]

    assert "-23,0-" in builder, "overnight windows must be split into two ranges"


def test_editing_warns_when_the_schedule_would_change():
    """Switching the Repeat dropdown regenerates the whole expression from that
    mode's controls. Opening the overnight sweep and touching the dropdown turns
    it into a single daily run -- the preview showed the new expression, but
    showing it is not the same as saying it replaced something."""
    markup = INDEX.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")

    assert 'id="mc-cron-warning"' in markup
    assert "renderCronChangeWarning" in renderer
    block = renderer.split("renderCronChangeWarning(expr)", 1)[1].split("syncCronBuilder()", 1)[0]
    # Both the old and the new schedule have to be named, or the warning says a
    # change happened without saying what it was.
    assert "Was:" in block and "Now:" in block


def test_the_warning_does_not_block_saving():
    """Changing the schedule is what Edit is for."""
    renderer = RENDERER.read_text(encoding="utf-8")
    save = renderer.split("async saveSchedule()", 1)[1].split("async toggleSchedule(", 1)[0]

    assert "_scheduleOriginalCron" not in save, "saving must not be gated on the warning"


def test_creating_a_schedule_does_not_warn():
    """There is nothing to have changed, and a warning on every new schedule is
    noise that trains the reader to ignore it."""
    renderer = RENDERER.read_text(encoding="utf-8")
    opener = renderer.split("openScheduleEditor(id = null)", 1)[1].split("closeScheduleEditor()", 1)[0]

    assert "job && !job.parse_error" in opener, "the baseline must only be set for a valid existing job"


def test_the_baseline_is_cleared_when_the_editor_closes():
    """A successful save closes the editor; a stale baseline would then warn
    about a change already committed."""
    renderer = RENDERER.read_text(encoding="utf-8")
    closer = renderer.split("closeScheduleEditor()", 1)[1].split("async saveSchedule()", 1)[0]

    assert "_scheduleOriginalCron = null" in closer


def test_delete_asks_first():
    source = RENDERER.read_text(encoding="utf-8")
    block = source.split("async deleteSchedule(", 1)[1].split("},", 1)[0]

    assert "confirm(" in block, "deleting a schedule must be confirmed"

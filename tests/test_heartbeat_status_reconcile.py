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


def test_delete_asks_first():
    source = RENDERER.read_text(encoding="utf-8")
    block = source.split("async deleteSchedule(", 1)[1].split("},", 1)[0]

    assert "confirm(" in block, "deleting a schedule must be confirmed"

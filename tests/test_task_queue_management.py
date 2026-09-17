"""
Manual control of the task queue.

task.clear TRUNCATEs the whole table and was the only removal that existed, so a
single stuck row could not be dropped without wiping every task and all of its
telemetry. There was no way to edit one either.

The load-bearing constraint is that the goal loop writes a task's status back
while it holds it, so an edit made mid-run appears to work and then silently
reverts -- a failure already paid for once in this project.
"""
import re
from pathlib import Path

import pytest

from python.harness.server import _HANDLERS
from python.harness.task_rpc import _TASK_LIVE_STATUSES

RENDERER = Path("renderer/renderer.js")
MISSION_OPS = Path("renderer/mission_control_ops.js")
INDEX = Path("renderer/index.html")
SERVER = Path("python/harness/task_rpc.py")  # task handlers moved here (size-ratchet cut-down)


# --- the RPCs exist and are reachable ---------------------------------------


@pytest.mark.parametrize("method", [
    "task.list", "task.submit", "task.control", "task.clear", "task.delete", "task.update",
])
def test_task_method_is_dispatchable(method):
    """A handler that is defined but never registered is unreachable, which is
    this codebase's most repeated defect."""
    assert method in _HANDLERS, f"{method} is defined but not in the dispatch table"


# --- the live-row guard -----------------------------------------------------


def test_live_statuses_are_shared_between_guard_and_ui():
    """The renderer decides whether to offer Edit or Abort using the same list
    the harness refuses on. If they drift, the UI offers an edit the server
    rejects."""
    renderer = RENDERER.read_text(encoding="utf-8")
    ui_list = re.search(r"TASK_LIVE_STATUSES:\s*\[([^\]]*)\]", renderer)

    assert ui_list, "the renderer must declare which statuses are live"
    ui_statuses = set(re.findall(r"'([a-z_]+)'", ui_list.group(1)))
    assert ui_statuses == set(_TASK_LIVE_STATUSES)


def test_editing_a_live_task_is_refused_with_a_reason():
    source = SERVER.read_text(encoding="utf-8")
    update = source.split("async def handle_task_update", 1)[1].split("async def handle_task_get_telemetry", 1)[0]

    assert "_TASK_LIVE_STATUSES" in update
    assert "silently revert" in update, "the refusal must explain why, not just refuse"
    assert "force" in update, "there must be a documented override"


def test_deleting_a_live_task_is_refused_by_default():
    source = SERVER.read_text(encoding="utf-8")
    delete = source.split("async def handle_task_delete", 1)[1].split("async def handle_task_update", 1)[0]

    assert "_TASK_LIVE_STATUSES" in delete
    assert "force" in delete


def test_delete_removes_children_rather_than_orphaning_them():
    """parent_id is ON DELETE SET NULL, so deleting a parent alone would leave
    its children in the queue as parentless rows nothing ever runs."""
    source = SERVER.read_text(encoding="utf-8")
    delete = source.split("async def handle_task_delete", 1)[1].split("async def handle_task_update", 1)[0]

    assert "parent_id = $1" in delete


def test_update_refuses_an_unknown_status():
    source = SERVER.read_text(encoding="utf-8")
    update = source.split("async def handle_task_update", 1)[1].split("async def handle_task_get_telemetry", 1)[0]

    assert "valid_statuses" in update


def test_update_does_not_touch_the_planned_graph():
    """Dependencies, payload and parentage describe a plan the goal engine is
    executing; rewriting them from the UI produces a graph it never planned."""
    source = SERVER.read_text(encoding="utf-8")
    update = source.split("async def handle_task_update", 1)[1].split("async def handle_task_get_telemetry", 1)[0]

    for column in ("dependencies", "payload", "parent_id"):
        assert f"{column} =" not in update, f"task.update must not rewrite {column}"


# --- the UI ------------------------------------------------------------------


def test_queue_controls_exist():
    markup = INDEX.read_text(encoding="utf-8")

    for element in ('id="btn-mc-task-new"', 'id="btn-mc-task-clear"',
                    'id="mc-task-editor"', 'id="mc-task-description"'):
        assert element in markup, f"the task queue panel is missing {element}"


def test_every_task_rpc_the_ui_needs_is_called():
    renderer = RENDERER.read_text(encoding="utf-8")
    ops = MISSION_OPS.read_text(encoding="utf-8")
    ui = renderer + "\n" + ops

    for method in ("task.submit", "task.update", "task.delete", "task.clear", "task.control"):
        assert f"'{method}'" in ui, f"the panel never calls {method}"


def test_destructive_task_actions_confirm_first():
    renderer = RENDERER.read_text(encoding="utf-8")
    ops = MISSION_OPS.read_text(encoding="utf-8")

    # deleteTask remains in renderer.js; clear lives in mission_control_ops.js
    del_block = renderer.split("async deleteTask(", 1)[1].split("async clearTaskQueue(", 1)[0]
    assert "confirm(" in del_block, "async deleteTask( must confirm before destroying anything"
    assert "confirm(" in ops.split("async function mcClearTaskQueue", 1)[1].split(
        "async function mcHaltCurrentLoop", 1
    )[0], "mcClearTaskQueue must confirm before destroying anything"


def test_clear_names_the_scale_of_what_it_destroys():
    """It is a TRUNCATE CASCADE of every task and all telemetry, not a tidy-up
    of finished rows."""
    ops = MISSION_OPS.read_text(encoding="utf-8")
    block = ops.split("async function mcClearTaskQueue", 1)[1].split(
        "async function mcHaltCurrentLoop", 1
    )[0]

    assert "telemetry" in block and "cannot be undone" in block
    assert "CLEAR_QUEUE" in block


def test_task_description_is_escaped_into_the_row():
    """It is free text, now typed through this very editor, rendered into
    innerHTML."""
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "${escapeHtml(t.description)}" in renderer
    # Scoped to innerHTML template literals. One plain-text use survives, where
    # the description is joined into a chat message rather than markup.
    for line in renderer.splitlines():
        if "${t.description}" in line:
            assert "appendMessage" in line or "`•" in line, (
                f"task description interpolated unescaped into markup: {line.strip()[:100]}"
            )

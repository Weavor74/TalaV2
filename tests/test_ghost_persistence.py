import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from python.harness.db import init_pool, close_pool

REPO_ROOT = Path(__file__).resolve().parent.parent
CHAIN_URL = "http://127.0.0.1:8332/chain/status"


def _chain_is_up() -> bool:
    try:
        with urllib.request.urlopen(CHAIN_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture
def ghost_chain_node(tmp_path, monkeypatch):
    """
    Ensure a Ghost Blockchain node is reachable on 127.0.0.1:8332 for the
    duration of the test.

    This test used to require the real app's chain node to already be
    running — on a dev box running only pytest it never was, so the test
    was permanently red ("unanchored"). app.js is dependency-free and
    resolves its data dir relative to its own location, so when no node is
    up we start an isolated copy in tmp_path with a fresh chain and tear it
    down afterwards. TALA_USER_DATA is pinned to tmp_path in that case so
    restore_from_ghost reads the same isolated chain the node writes.

    If a live node is already up (the running Tala app), it is used as-is —
    that is this test's original, intended integration mode.
    """
    if _chain_is_up():
        yield "live"
        return

    if shutil.which("node") is None:
        pytest.skip("node.js unavailable and no chain node running on 8332")

    # The Python client hardcodes 127.0.0.1:8332, so the port must be free
    # (we only get here when nothing answered /chain/status; a non-chain
    # process squatting the port would still collide).
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8332)) == 0:
            pytest.skip("port 8332 occupied by something that is not a chain node")

    src_dir = tmp_path / "blockchain" / "src"
    data_dir = tmp_path / "blockchain" / "data"
    src_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "blockchain" / "src" / "app.js", src_dir / "app.js")

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))

    # stdin must be a held-open pipe: app.js watches its parent pipe (for
    # Electron child management) and exits the moment stdin closes.
    proc = subprocess.Popen(
        ["node", str(src_dir / "app.js")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", errors="replace")
                pytest.fail(f"isolated chain node exited early:\n{out}")
            if _chain_is_up():
                break
            time.sleep(0.25)
        else:
            pytest.fail("isolated chain node never became reachable on 8332")

        yield "isolated"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_ghost_persistence(ghost_chain_node):
    # Imported here so the fixture's TALA_USER_DATA pin is active before any
    # module-level path resolution the import chain might do.
    from sdk.agent.ghost_persistence import commit_to_ghost, restore_from_ghost

    # Initialize DB pool
    await init_pool()
    try:
        task_id = "test-task-id-12345"
        context = {"idx": 0, "parent": "goal-parent"}
        var_state = {"phases": ["phase 1"]}
        next_step_hash = "abc123nextstep"

        # Commit to ghost
        tx_id = await commit_to_ghost(task_id, context, var_state, next_step_hash)
        assert tx_id != "unanchored"

        # Restore from ghost
        restored = await restore_from_ghost(task_id)
        assert restored is not None
        assert restored["task_id"] == task_id
        assert restored["process_context"]["parent"] == "goal-parent"
        assert restored["variable_state"]["phases"] == ["phase 1"]
        assert restored["intended_next_step_hash"] == next_step_hash
    finally:
        await close_pool()

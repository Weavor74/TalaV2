import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from python.harness.db import init_pool, close_pool

REPO_ROOT = Path(__file__).resolve().parent.parent
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def ghost_chain_node(tmp_path, monkeypatch):
    """
    Start a Ghost Blockchain node of this test's own, on a free port, over a
    fresh chain in tmp_path, and point the Python client at it.

    This used to adopt the real app's node whenever one was up on 8332. That
    was the leak: the test's TALA_USER_DATA sandbox minted its own document key
    and the thought-state it committed was sealed under it and written to the
    live chain, where it can never be opened. The test now never touches a node
    it did not start.
    """
    if shutil.which("node") is None:
        pytest.skip("node.js unavailable")

    port = _free_port()
    chain_url = f"http://127.0.0.1:{port}/chain/status"

    def _chain_is_up() -> bool:
        try:
            with urllib.request.urlopen(chain_url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    src_dir = tmp_path / "blockchain" / "src"
    data_dir = tmp_path / "blockchain" / "data"
    src_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    # The node is app.js plus its sibling modules; copying app.js alone dies on require().
    for module in (REPO_ROOT / "blockchain" / "src").glob("*.js"):
        shutil.copy2(module, src_dir / module.name)

    monkeypatch.setenv("TALA_USER_DATA", str(tmp_path))
    monkeypatch.setenv("TALA_CHAIN_BIND_PORT", str(port))
    monkeypatch.setenv("TALA_CHAIN_DATA_DIR", str(data_dir))

    # stdin must be a held-open pipe: app.js watches its parent pipe (for
    # Electron child management) and exits the moment stdin closes.
    proc = subprocess.Popen(
        ["node", str(src_dir / "app.js")],
        env={**os.environ},
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
            pytest.fail(f"isolated chain node never became reachable on {port}")

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

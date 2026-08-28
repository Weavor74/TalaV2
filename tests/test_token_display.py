"""
The status bar was showing consumption and calling it a balance.

The number in the status bar came from `SUM(prompt_tokens + completion_tokens)`
— every LLM token ever spent, a figure that only grows — under the label
"Tokens", into a renderer function called `setTokenBalance` (UI-001).

The giveaway was already in the code: that function branches on `999999`, the
ghost sentinel returned by the *wallet*. A monotonically growing consumption
total can never equal it, so the branch had never once fired (UI-002).

Underneath sat a second conflation. `get_wallet_balance()` returns that same
sentinel both when tokens really are unlimited and when the chain could not be
reached, so "you have infinite tokens" and "nobody could be asked" were the
same answer.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest

from python.harness import token_counter


@pytest.fixture
def mode(monkeypatch):
    """Pin the connection mode without touching the database."""
    state = {"value": "local"}

    async def get_mode():
        return state["value"]
    monkeypatch.setattr(token_counter, "_get_system_connection_mode", get_mode)
    return state


# -- the wallet says which situation produced its answer ----------------------

@pytest.mark.asyncio
async def test_local_mode_is_unlimited(mode):
    state = await token_counter.wallet_state()

    assert state["mode"] == "ghost"
    assert state["unlimited"] is True
    assert state["balance"] == token_counter._GHOST_BALANCE


@pytest.mark.asyncio
async def test_an_unreachable_chain_is_not_unlimited(mode, monkeypatch):
    """The conflation that mattered: the status bar would have reported
    unlimited tokens while the truth was that nobody could be asked."""
    mode["value"] = "vps"

    state = await token_counter.wallet_state()

    assert state["mode"] == "unreachable"
    assert state["unlimited"] is False
    assert state["balance"] is None, "an unknown balance is not a balance"
    assert state["reason"]


@pytest.mark.asyncio
async def test_a_real_balance_is_reported_as_one(mode, monkeypatch):
    mode["value"] = "vps"

    class Response:
        def read(self):
            return json.dumps({"balance": 12.5}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(token_counter.urllib.request, "urlopen",
                        lambda *a, **k: Response())

    state = await token_counter.wallet_state()

    assert state == {"balance": 12.5, "mode": "real", "unlimited": False}


# -- the health payload carries both numbers ----------------------------------

def test_health_reports_a_balance_as_well_as_consumption():
    from python.harness import server

    source = inspect.getsource(server.handle_harness_health)
    tree = ast.parse(source)
    keys = {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert "token_balance" in keys, "the balance is still not reported"
    assert "token_mode" in keys, "the mode is still left to be guessed"
    assert "tokens_used" in keys, "consumption was dropped rather than relabelled"


def test_health_reads_the_wallet():
    from python.harness import server

    tree = ast.parse(inspect.getsource(server.handle_harness_health))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "wallet_state" in called


# -- the renderer ------------------------------------------------------------

def _renderer() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "renderer" / "renderer.js"
            ).read_text(encoding="utf-8")


def test_the_status_bar_no_longer_sniffs_the_sentinel():
    """Inferring the mode from `999999` reported unlimited tokens whenever the
    chain was unreachable, since the wallet returns that for both."""
    source = _renderer()
    start = source.index("setTokenBalance(state) {")
    body = source[start:source.index("setTokensUsed(", start)]

    assert "999999" not in body
    assert "mode === 'ghost'" in body
    assert "unreachable" in body


def test_the_dead_sidebar_bar_is_gone():
    """`#sidebar-token-balance` and `#token-bar` were written to on every
    health tick and appear nowhere in the markup — so the 1,000,000 maximum
    UI-003 reports was scaling a bar that does not exist."""
    from pathlib import Path

    renderer = Path(__file__).resolve().parents[1] / "renderer"
    source = (renderer / "renderer.js").read_text(encoding="utf-8")
    markup = (renderer / "index.html").read_text(encoding="utf-8")

    # Matched as selectors, not substrings: `status-tokens-used` is the live
    # element and contains one of the dead names.
    for element in ("sidebar-token-balance", "token-bar", "tokens-used"):
        assert f"qs('#{element}')" not in source, f"{element} is still written to"
        assert f'id="{element}"' not in markup


def test_consumption_has_a_place_of_its_own():
    from pathlib import Path

    markup = (Path(__file__).resolve().parents[1] / "renderer" / "index.html"
              ).read_text(encoding="utf-8")

    assert 'id="status-tokens-used"' in markup
    assert "setTokensUsed" in _renderer()


# -- the chain viewer ---------------------------------------------------------

def test_the_viewer_resolves_the_node_address():
    """A portable install can move the port, and the hardcoded one is only
    right until somebody does (UI-025)."""
    source = _renderer()
    start = source.index("const BlockchainViewer = {")
    body = source[start:source.index("\n};", start)]

    assert "chain.summary" in body
    assert "ghost_endpoint" in body
    # the literal survives only as a fallback, not as the two fetch targets
    assert "fetch('http://127.0.0.1:8332/chain/status')" not in body


def test_transaction_ids_are_not_clipped():
    """Eight characters turned every `settings-*` id into `settings` and
    `ghost_commit_…` into `ghost_co`, collapsing unrelated transactions onto
    one string and destroying the only ghost marker the card had (UI-026)."""
    source = _renderer()

    assert "tx.id.slice(0, 8)" not in source
    assert "UUID: ${" not in source


def test_chain_data_is_escaped_before_it_reaches_the_dom():
    """Reconcile pulls transactions written by other people, and `creator` is
    whatever they put in it."""
    source = _renderer()
    start = source.index("block.transactions.forEach(tx => {")
    body = source[start:source.index("});", start)]

    for field in ("tx.id", "tx.creator", "tx.hash", "tx.type"):
        assert f"escapeHtml(String({field}" in body, f"{field} is unescaped"


# -- the second chain implementation is gone ----------------------------------

def test_the_python_ledger_is_deleted():
    """Two complete chain servers targeting the same port, one of them with no
    callers (UI-027)."""
    from pathlib import Path

    assert not (Path(__file__).resolve().parents[1] / "python" / "ledger").exists()


def test_nothing_imports_it():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "python").rglob("*.py")) + list((root / "sdk").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "from python.ledger" in text or "import python.ledger" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders

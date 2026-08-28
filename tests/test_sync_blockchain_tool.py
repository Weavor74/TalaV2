"""
The sync_blockchain tool, after the reconcile fix (UI-124).

These tests previously asserted the old premise: that a difference between the
local chain's latest hash and a remote chain's latest hash meant the two had
diverged and needed syncing. That premise does not survive the Ghost/Real
split. They are two chains divided by audience, not two copies of one chain —
Ghost holds everything this machine did and Real holds only what involved
somebody else — so they differ permanently and by design. Under the old rule
every run reported a divergence, and the sync it triggered overwrote the local
chain with the remote one.

So the gate now asks a question that can actually be answered wrongly: is the
local chain reachable, and does it verify?
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import AsyncMock, patch

import pytest

from sdk.agent.tools import sync_blockchain


@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.get_local_status")
async def test_a_healthy_local_chain_needs_no_sync(mock_local):
    """The ordinary state, and the one the tool must refuse."""
    mock_local.return_value = {"latestBlockHash": "hash123", "height": 5,
                               "isValid": True}

    with pytest.raises(PermissionError) as raised:
        await sync_blockchain()

    assert "No 'Divergence-Detected' signal present" in str(raised.value)


@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.get_local_status")
async def test_a_taller_remote_chain_is_not_a_divergence(mock_local):
    """
    Ghost being a different height from the shared chain is the design. It was
    the trigger for the overwrite that would have destroyed local history.
    """
    mock_local.return_value = {"latestBlockHash": "hash_local", "height": 5,
                               "isValid": True}

    with pytest.raises(PermissionError):
        await sync_blockchain()


@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation",
       new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.get_local_status")
async def test_a_local_chain_that_does_not_verify_is_reconciled(
        mock_local, mock_reconcile):
    """The one condition genuinely worth acting on."""
    mock_local.return_value = {"latestBlockHash": "hash_local", "height": 5,
                               "isValid": False}
    mock_reconcile.return_value = True

    result = await sync_blockchain()

    assert "reconciliation successful" in result
    assert mock_reconcile.call_count == 1


@pytest.mark.asyncio
@patch("sdk.agent.ghost_persistence.pre_flight_reconciliation",
       new_callable=AsyncMock)
@patch("sdk.agent.ghost_persistence.get_local_status")
async def test_an_unreachable_local_chain_is_reconciled(
        mock_local, mock_reconcile):
    mock_local.return_value = {"error": "connection refused"}
    mock_reconcile.return_value = True

    result = await sync_blockchain()

    assert "reconciliation successful" in result


def _module_ast():
    import ast
    import inspect

    from sdk.agent import ghost_persistence

    return ast.parse(inspect.getsource(ghost_persistence))


def test_the_shared_chain_endpoint_is_not_hardcoded():
    """A portable offline-first install must not carry a single external
    domain as an implicit dependency — least of all one whose response is fed
    into the local chain.

    Checked against operational string literals rather than the source text:
    the comment and docstring explaining the fix both name the domain they
    removed, and prose describing a defect is not the defect.
    """
    import ast

    tree = _module_ast()
    docstrings = {node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Constant)}

    offenders = [node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant)
                 and isinstance(node.value, str)
                 and node not in docstrings
                 and "talacore" in node.value]
    assert not offenders, offenders


def test_certificates_are_verified():
    """The remote fetch ran with check_hostname off and CERT_NONE, so anything
    on the path could answer as the shared chain — and what came back was
    chain data headed for the local node.

    Assigning to either attribute of an SSL context is the defect, so that is
    what is looked for, not the words.
    """
    import ast

    disabled = []
    for node in ast.walk(_module_ast()):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and target.attr in ("check_hostname", "verify_mode")):
                disabled.append(target.attr)
    assert not disabled, f"SSL verification is being weakened: {disabled}"


def test_the_local_ghost_chain_is_never_written_by_a_reconcile():
    """pre_flight_reconciliation used to POST remote blocks into the local
    node on every goal run, which under the old overwrite rule replaced the
    complete local history."""
    import inspect

    from sdk.agent import ghost_persistence

    import ast

    tree = ast.parse(inspect.getsource(ghost_persistence.pre_flight_reconciliation))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_post_json" not in called, "it still writes to a chain node"

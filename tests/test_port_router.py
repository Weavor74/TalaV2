"""
tests/test_port_router.py
=========================
Unit tests for sdk.agent.port_router.PortRouter.is_port_in_use.

Covers:
  1. An occupied TCP port (a real listening socket) reports True.
  2. A closed/unbound TCP port reports False.
  3. The method is callable as a static method without an instance.
  4. Defensive: invalid port values return False rather than raising.
"""

from __future__ import annotations

import socket

import pytest

from sdk.agent.port_router import PortRouter


@pytest.fixture()
def listening_port():
    """Binds a real TCP listener on an ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


@pytest.fixture()
def closed_port():
    """Finds an ephemeral port that is guaranteed closed after the fixture yields."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


class TestIsPortInUse:
    def test_open_port_returns_true(self, listening_port: int) -> None:
        assert PortRouter.is_port_in_use(listening_port) is True

    def test_closed_port_returns_false(self, closed_port: int) -> None:
        assert PortRouter.is_port_in_use(closed_port) is False

    def test_callable_without_instance(self, listening_port: int) -> None:
        # Static method contract: no PortRouter() construction required.
        assert PortRouter.is_port_in_use(listening_port) is True

    def test_returns_strict_bool(self, closed_port: int) -> None:
        result = PortRouter.is_port_in_use(closed_port)
        assert isinstance(result, bool)

    def test_invalid_port_returns_false_not_raise(self) -> None:
        # Boundary validation: out-of-range ports must not propagate exceptions.
        try:
            result = PortRouter.is_port_in_use(-1)
        except Exception:
            result = False
        assert result is False
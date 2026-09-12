"""
sdk/agent/leases.py
===================
Controlled Leases, Status Reports & Contracts — Tala Cognitive Engine Room
Milestone 6 | Task 6.5.1

Provides:
  • LeaseRequest    — dataclass for gate-controlled operations
  • LeaseGateway    — stores pending leases, requires user sign-off
  • LeaseToken      — represents an approved (or pending) lease
  • SystemStatusLog — structured application status logger
  • StatusReport    — dataclass for component health vectors
  • TelemetryChannel— emits status reports to Electron frontend via WebSocket

Gate-controlled operations:
  'ledger'    — any write to the PostgreSQL financial ledger
  'file_edit' — any filesystem mutation outside the agent's sandbox
  'wallet'    — any wallet balance or transaction operation

A lease MUST be approved before the guarded operation executes.
Unapproved leases that time-out leave the operation in BLOCKED state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

OperationType = str  # 'ledger' | 'file_edit' | 'wallet'
VALID_OPERATIONS: frozenset[str] = frozenset({"ledger", "file_edit", "wallet"})

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StatusLevel(Enum):
    """Application vector health levels."""
    COMPLETE = "COMPLETE"      # All checks pass, fully operational
    PARTIAL = "PARTIAL"        # Core functionality works; non-critical gaps
    DEGRADED = "DEGRADED"      # Significant capability loss; usable but impaired
    UNVERIFIED = "UNVERIFIED"  # Status unknown or checks not yet run


class LeaseState(Enum):
    PENDING = auto()
    APPROVED = auto()
    DENIED = auto()
    EXPIRED = auto()
    REVOKED = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LeaseRequest:
    """A request to perform a gate-controlled operation."""

    operation_type: OperationType
    description: str
    requested_by: str  # Agent ID or component name
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation_type not in VALID_OPERATIONS:
            raise ValueError(
                f"operation_type {self.operation_type!r} is not valid. "
                f"Choose from: {sorted(VALID_OPERATIONS)}"
            )
        if not self.description.strip():
            raise ValueError("LeaseRequest.description must not be empty")
        if not self.requested_by.strip():
            raise ValueError("LeaseRequest.requested_by must not be empty")


@dataclass
class LeaseToken:
    """Represents the approval state of a LeaseRequest."""

    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request: LeaseRequest | None = None
    approved: bool = False
    denied: bool = False
    approved_at: datetime | None = None
    denied_at: datetime | None = None
    expires_at: datetime | None = None
    revoked: bool = False
    revoke_reason: str = ""
    _state: LeaseState = field(default=LeaseState.PENDING, init=False, repr=False)

    def is_valid(self) -> bool:
        """True if the token is approved, not expired, and not revoked."""
        if not self.approved or self.denied or self.revoked:
            return False
        if self.expires_at and datetime.now(UTC) > self.expires_at:
            return False
        return True

    def is_expired(self) -> bool:
        """True if the token has passed its expiry time."""
        return bool(self.expires_at and datetime.now(UTC) > self.expires_at)

    def state(self) -> LeaseState:
        if self.revoked:
            return LeaseState.REVOKED
        if self.denied:
            return LeaseState.DENIED
        if self.is_expired():
            return LeaseState.EXPIRED
        if self.approved:
            return LeaseState.APPROVED
        return LeaseState.PENDING


@dataclass
class StatusReport:
    """Health report for a single application component or service."""

    component: str
    status: StatusLevel
    detail: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "detail": self.detail,
            "timestamp": self.timestamp.isoformat(),
            "metrics": self.metrics,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# LeaseGateway
# ---------------------------------------------------------------------------


class LeaseGateway:
    """
    Stores and manages pending leases.

    Workflow
    --------
    1. Agent calls request_lease(op) → gets a LeaseToken in PENDING state.
    2. The LeaseToken.token_id is surfaced to the user (via UI / WebSocket).
    3. User approves via approve_lease(token_id) or denies via deny_lease().
    4. Agent calls await_approval(token) → blocks until approved, denied, or
       timeout.
    5. If approved, the guarded operation proceeds.  Otherwise it is aborted.

    Thread Safety
    -------------
    All state mutations are protected by an internal threading.Lock, making
    the gateway safe for concurrent use from multiple agent threads.
    """

    def __init__(
        self,
        default_ttl_seconds: int = 300,
        approval_callback: Callable[[LeaseToken], None] | None = None,
    ) -> None:
        self._ttl = default_ttl_seconds
        self._approval_callback = approval_callback
        self._leases: dict[str, LeaseToken] = {}
        self._lock = threading.Lock()
        self._approval_events: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_lease(
        self,
        op: LeaseRequest,
        ttl_seconds: int | None = None,
    ) -> LeaseToken:
        """
        Register a new LeaseRequest and return a pending LeaseToken.

        The token starts in PENDING state.  The caller must then call
        await_approval() to wait for user sign-off.

        Parameters
        ----------
        op:
            The LeaseRequest describing the guarded operation.
        ttl_seconds:
            Override the gateway's default TTL for this lease.

        Returns
        -------
        LeaseToken in PENDING state.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)

        token = LeaseToken(
            request=op,
            approved=False,
            denied=False,
            expires_at=expires_at,
        )

        with self._lock:
            self._leases[token.token_id] = token
            evt = threading.Event()
            self._approval_events[token.token_id] = evt

        log.info(
            "LeaseGateway: lease %s requested [%s] by %s — expires %s",
            token.token_id[:8],
            op.operation_type,
            op.requested_by,
            expires_at.isoformat(),
        )

        # Notify callback (e.g. emit to WebSocket for UI display)
        if self._approval_callback:
            try:
                self._approval_callback(token)
            except Exception as exc:
                log.warning("approval_callback raised: %s", exc)

        return token

    def approve_lease(self, token_id: str, approved_by: str = "user") -> bool:
        """
        Mark the lease identified by *token_id* as approved.

        Returns False if the token is not found, already decided, or expired.
        """
        with self._lock:
            token = self._leases.get(token_id)
            if not token:
                log.warning("approve_lease: token %r not found", token_id)
                return False
            if token.denied or token.revoked:
                log.warning("approve_lease: token %r is already decided", token_id)
                return False
            if token.is_expired():
                log.warning("approve_lease: token %r has expired", token_id)
                return False

            token.approved = True
            token.approved_at = datetime.now(UTC)
            evt = self._approval_events.get(token_id)

        log.info(
            "LeaseGateway: lease %s APPROVED by %s",
            token_id[:8], approved_by,
        )
        if evt:
            evt.set()
        return True

    def deny_lease(self, token_id: str, reason: str = "") -> bool:
        """
        Mark the lease identified by *token_id* as denied.
        """
        with self._lock:
            token = self._leases.get(token_id)
            if not token:
                log.warning("deny_lease: token %r not found", token_id)
                return False
            if token.approved or token.revoked:
                log.warning("deny_lease: token %r already decided", token_id)
                return False

            token.denied = True
            token.denied_at = datetime.now(UTC)
            if reason:
                token.revoke_reason = reason
            evt = self._approval_events.get(token_id)

        log.info(
            "LeaseGateway: lease %s DENIED — %s",
            token_id[:8], reason or "no reason given",
        )
        if evt:
            evt.set()
        return True

    def revoke_lease(self, token_id: str, reason: str = "") -> bool:
        """Revoke a previously-approved lease."""
        with self._lock:
            token = self._leases.get(token_id)
            if not token:
                return False
            token.revoked = True
            token.revoke_reason = reason
            evt = self._approval_events.get(token_id)

        log.info("LeaseGateway: lease %s REVOKED — %s", token_id[:8], reason)
        if evt:
            evt.set()
        return True

    def await_approval(
        self,
        lease_token: LeaseToken,
        timeout_seconds: int = 60,
    ) -> bool:
        """
        Block until the lease is approved, denied, expired, or *timeout* elapses.

        Parameters
        ----------
        lease_token:
            The LeaseToken returned by request_lease().
        timeout_seconds:
            Maximum time to wait.  Note this is a soft timeout in addition to
            the token's own expires_at; whichever comes first wins.

        Returns
        -------
        bool
            True if the lease was approved and is still valid, False otherwise.
        """
        with self._lock:
            evt = self._approval_events.get(lease_token.token_id)

        if evt is None:
            log.warning(
                "await_approval: no event found for token %s",
                lease_token.token_id[:8],
            )
            return False

        # Compute effective wait: minimum of caller timeout and token TTL
        now = datetime.now(UTC)
        if lease_token.expires_at:
            remaining_ttl = (lease_token.expires_at - now).total_seconds()
            effective_timeout = min(timeout_seconds, max(0.0, remaining_ttl))
        else:
            effective_timeout = float(timeout_seconds)

        log.debug(
            "await_approval: waiting up to %.1fs for lease %s",
            effective_timeout, lease_token.token_id[:8],
        )
        signalled = evt.wait(timeout=effective_timeout)

        if not signalled:
            log.warning(
                "await_approval: lease %s timed out after %.1fs",
                lease_token.token_id[:8], effective_timeout,
            )

        result = lease_token.is_valid()
        log.info(
            "await_approval: lease %s → %s",
            lease_token.token_id[:8], "APPROVED" if result else "NOT APPROVED",
        )
        return result

    def get_token(self, token_id: str) -> LeaseToken | None:
        with self._lock:
            return self._leases.get(token_id)

    def pending_leases(self) -> list[LeaseToken]:
        """Return all leases currently in PENDING state."""
        with self._lock:
            return [
                t for t in self._leases.values()
                if t.state() == LeaseState.PENDING
            ]

    def purge_expired(self) -> int:
        """Remove expired leases from the registry. Returns count removed."""
        with self._lock:
            expired_ids = [
                tid for tid, t in self._leases.items()
                if t.is_expired() and t.state() == LeaseState.EXPIRED
            ]
            for tid in expired_ids:
                del self._leases[tid]
                self._approval_events.pop(tid, None)
        log.debug("LeaseGateway: purged %d expired lease(s)", len(expired_ids))
        return len(expired_ids)


# ---------------------------------------------------------------------------
# SystemStatusLog
# ---------------------------------------------------------------------------


class SystemStatusLog:
    """
    Structured logger for application vector health status.

    Maintains an in-memory ring buffer of the most recent status reports
    per component, and exposes them for reporting / WebSocket emission.
    """

    def __init__(self, max_history: int = 200) -> None:
        self._history: list[StatusReport] = []
        self._max_history = max_history
        self._latest: dict[str, StatusReport] = {}
        self._lock = threading.Lock()

    def log(self, report: StatusReport) -> None:
        """Record a new StatusReport."""
        with self._lock:
            self._history.append(report)
            if len(self._history) > self._max_history:
                self._history.pop(0)
            self._latest[report.component] = report

        level_map = {
            StatusLevel.COMPLETE: logging.INFO,
            StatusLevel.PARTIAL: logging.WARNING,
            StatusLevel.DEGRADED: logging.WARNING,
            StatusLevel.UNVERIFIED: logging.DEBUG,
        }
        lvl = level_map.get(report.status, logging.INFO)
        log.log(
            lvl,
            "SystemStatus [%s] %s: %s",
            report.component, report.status.value, report.detail,
        )

    def latest(self, component: str) -> StatusReport | None:
        """Return the most recent StatusReport for *component*."""
        with self._lock:
            return self._latest.get(component)

    def all_latest(self) -> dict[str, StatusReport]:
        """Return the most recent report for every component."""
        with self._lock:
            return dict(self._latest)

    def degraded_components(self) -> list[StatusReport]:
        """Return components currently in DEGRADED or UNVERIFIED state."""
        with self._lock:
            return [
                r for r in self._latest.values()
                if r.status in (StatusLevel.DEGRADED, StatusLevel.UNVERIFIED)
            ]

    def history(self, limit: int = 50) -> list[StatusReport]:
        """Return the last *limit* reports."""
        with self._lock:
            return list(self._history[-limit:])

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary of current system health."""
        all_reports = self.all_latest()
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "component_count": len(all_reports),
            "components": {
                name: report.to_dict()
                for name, report in all_reports.items()
            },
            "degraded_count": len(self.degraded_components()),
        }


# ---------------------------------------------------------------------------
# TelemetryChannel
# ---------------------------------------------------------------------------


class TelemetryChannel:
    """
    Emits StatusReport objects to the Electron frontend over WebSocket.

    The channel maintains a non-blocking outbound queue and uses a background
    daemon thread to drain it.  If no WebSocket clients are connected, emits
    are silently dropped (no backpressure into the agent loop).

    Parameters
    ----------
    host:
        WebSocket server host.  Defaults to 'localhost'.
    port:
        WebSocket server port.  Defaults to 9765 (Tala telemetry port).
    status_log:
        Optional SystemStatusLog instance to record all emitted reports.
    """

    _TALA_TELEMETRY_PORT = 9765

    def __init__(
        self,
        host: str = "localhost",
        port: int | None = None,
        status_log: SystemStatusLog | None = None,
        auto_reconnect: bool = True,
    ) -> None:
        self._host = host
        self._port = port or self._TALA_TELEMETRY_PORT
        self._status_log = status_log
        self._auto_reconnect = auto_reconnect
        self._connected = False
        self._ws = None  # websockets.WebSocketClientProtocol when connected
        self._queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._sender_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def emit(self, report: StatusReport) -> None:
        """
        Queue a StatusReport for emission to the Electron frontend.

        Returns immediately — emission is asynchronous.
        The report is also written to the SystemStatusLog if one is configured.

        Parameters
        ----------
        report:
            The StatusReport to emit.
        """
        if self._status_log:
            self._status_log.log(report)

        payload = json.dumps({
            "type": "telemetry",
            "payload": report.to_dict(),
        })

        with self._queue_lock:
            self._queue.append(payload)

        # Start the sender thread lazily on first emit
        if self._sender_thread is None or not self._sender_thread.is_alive():
            self._start_sender()

        log.debug(
            "TelemetryChannel.emit: queued [%s] %s",
            report.status.value, report.component,
        )

    def emit_many(self, reports: list[StatusReport]) -> None:
        """Emit multiple reports in one call."""
        for report in reports:
            self.emit(report)

    def stop(self, timeout: float = 2.0) -> None:
        """Gracefully stop the background sender thread."""
        self._stop_event.set()
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _start_sender(self) -> None:
        """Start the background WebSocket sender thread."""
        self._stop_event.clear()
        self._sender_thread = threading.Thread(
            target=self._send_loop,
            daemon=True,
            name="tala-telemetry-sender",
        )
        self._sender_thread.start()
        log.debug("TelemetryChannel: sender thread started")

    def _send_loop(self) -> None:
        """
        Background loop: drain the queue and send each payload via WebSocket.

        Uses synchronous socket I/O to avoid asyncio event loop conflicts with
        the Electron main process bridge.  Falls back to logging if no server
        is reachable.
        """

        while not self._stop_event.is_set():
            with self._queue_lock:
                if not self._queue:
                    time.sleep(0.05)
                    continue
                payload = self._queue.pop(0)

            try:
                self._send_raw(payload)
            except OSError as exc:
                # Server not reachable — log and drop
                log.debug(
                    "TelemetryChannel: WebSocket send failed (%s) — payload dropped",
                    exc,
                )
            except Exception as exc:
                log.warning("TelemetryChannel: unexpected send error: %s", exc)

        log.debug("TelemetryChannel: sender thread stopped")

    def _send_raw(self, payload: str) -> None:
        """
        Send *payload* as a WebSocket text frame.

        Implements a minimal WebSocket text-frame handshake over raw TCP.
        For production use, replace with the `websockets` library.
        """
        import base64
        import socket as _socket
        import struct

        # HTTP upgrade handshake
        key = base64.b64encode(uuid.uuid4().bytes).decode()
        handshake = (
            f"GET /telemetry HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )

        with _socket.create_connection(
            (self._host, self._port), timeout=2.0
        ) as sock:
            sock.sendall(handshake.encode())
            response = sock.recv(4096).decode(errors="replace")
            if "101 Switching Protocols" not in response:
                log.debug("TelemetryChannel: WebSocket upgrade failed — no server")
                return

            # Encode payload as a WebSocket text frame (RFC 6455 §5.2)
            data = payload.encode("utf-8")
            length = len(data)
            mask_bit = 0x80  # clients must mask frames
            mask_key = uuid.uuid4().bytes[:4]

            if length <= 125:
                header = struct.pack("BB", 0x81, mask_bit | length)
            elif length <= 65535:
                header = struct.pack("!BBH", 0x81, mask_bit | 126, length)
            else:
                header = struct.pack("!BBQ", 0x81, mask_bit | 127, length)

            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            sock.sendall(header + mask_key + masked)
            log.debug(
                "TelemetryChannel: sent %d byte(s) to ws://%s:%d/telemetry",
                len(payload), self._host, self._port,
            )

# File: `leases.py`

## Module Description

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

## Classes

### `class StatusLevel`
Application vector health levels.


### `class LeaseState`

### `class LeaseRequest`
A request to perform a gate-controlled operation.

- **`def __post_init__(self)`**

### `class LeaseToken`
Represents the approval state of a LeaseRequest.

- **`def is_valid(self)`**
  - True if the token is approved, not expired, and not revoked.
- **`def is_expired(self)`**
  - True if the token has passed its expiry time.
- **`def state(self)`**

### `class StatusReport`
Health report for a single application component or service.

- **`def to_dict(self)`**
- **`def to_json(self)`**

### `class LeaseGateway`
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

- **`def __init__(self, default_ttl_seconds, approval_callback)`**
- **`def request_lease(self, op, ttl_seconds)`**
  - Register a new LeaseRequest and return a pending LeaseToken.
  
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
- **`def approve_lease(self, token_id, approved_by)`**
  - Mark the lease identified by *token_id* as approved.
  
  Returns False if the token is not found, already decided, or expired.
- **`def deny_lease(self, token_id, reason)`**
  - Mark the lease identified by *token_id* as denied.
- **`def revoke_lease(self, token_id, reason)`**
  - Revoke a previously-approved lease.
- **`def await_approval(self, lease_token, timeout_seconds)`**
  - Block until the lease is approved, denied, expired, or *timeout* elapses.
  
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
- **`def get_token(self, token_id)`**
- **`def pending_leases(self)`**
  - Return all leases currently in PENDING state.
- **`def purge_expired(self)`**
  - Remove expired leases from the registry. Returns count removed.

### `class SystemStatusLog`
Structured logger for application vector health status.

Maintains an in-memory ring buffer of the most recent status reports
per component, and exposes them for reporting / WebSocket emission.

- **`def __init__(self, max_history)`**
- **`def log(self, report)`**
  - Record a new StatusReport.
- **`def latest(self, component)`**
  - Return the most recent StatusReport for *component*.
- **`def all_latest(self)`**
  - Return the most recent report for every component.
- **`def degraded_components(self)`**
  - Return components currently in DEGRADED or UNVERIFIED state.
- **`def history(self, limit)`**
  - Return the last *limit* reports.
- **`def summary(self)`**
  - Return a JSON-serialisable summary of current system health.

### `class TelemetryChannel`
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

- **`def __init__(self, host, port, status_log, auto_reconnect)`**
- **`def emit(self, report)`**
  - Queue a StatusReport for emission to the Electron frontend.
  
  Returns immediately — emission is asynchronous.
  The report is also written to the SystemStatusLog if one is configured.
  
  Parameters
  ----------
  report:
      The StatusReport to emit.
- **`def emit_many(self, reports)`**
  - Emit multiple reports in one call.
- **`def stop(self, timeout)`**
  - Gracefully stop the background sender thread.
- **`def _start_sender(self)`**
  - Start the background WebSocket sender thread.
- **`def _send_loop(self)`**
  - Background loop: drain the queue and send each payload via WebSocket.
  
  Uses synchronous socket I/O to avoid asyncio event loop conflicts with
  the Electron main process bridge.  Falls back to logging if no server
  is reachable.
- **`def _send_raw(self, payload)`**
  - Send *payload* as a WebSocket text frame.
  
  Implements a minimal WebSocket text-frame handshake over raw TCP.
  For production use, replace with the `websockets` library.

## Source Code

```python
"""\nsdk/agent/leases.py\n===================\nControlled Leases, Status Reports & Contracts — Tala Cognitive Engine Room\nMilestone 6 | Task 6.5.1\n\nProvides:\n  • LeaseRequest    — dataclass for gate-controlled operations\n  • LeaseGateway    — stores pending leases, requires user sign-off\n  • LeaseToken      — represents an approved (or pending) lease\n  • SystemStatusLog — structured application status logger\n  • StatusReport    — dataclass for component health vectors\n  • TelemetryChannel— emits status reports to Electron frontend via WebSocket\n\nGate-controlled operations:\n  'ledger'    — any write to the PostgreSQL financial ledger\n  'file_edit' — any filesystem mutation outside the agent's sandbox\n  'wallet'    — any wallet balance or transaction operation\n\nA lease MUST be approved before the guarded operation executes.\nUnapproved leases that time-out leave the operation in BLOCKED state.\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport json\nimport logging\nimport threading\nimport time\nimport uuid\nfrom collections.abc import Callable\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime, timedelta\nfrom enum import Enum, auto\nfrom typing import Any\n\nlog = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Type aliases\n# ---------------------------------------------------------------------------\n\nOperationType = str  # 'ledger' | 'file_edit' | 'wallet'\nVALID_OPERATIONS: frozenset[str] = frozenset({"ledger", "file_edit", "wallet"})\n\n# ---------------------------------------------------------------------------\n# Enums\n# ---------------------------------------------------------------------------\n\n\nclass StatusLevel(Enum):\n    """Application vector health levels."""\n    COMPLETE = "COMPLETE"      # All checks pass, fully operational\n    PARTIAL = "PARTIAL"        # Core functionality works; non-critical gaps\n    DEGRADED = "DEGRADED"      # Significant capability loss; usable but impaired\n    UNVERIFIED = "UNVERIFIED"  # Status unknown or checks not yet run\n\n\nclass LeaseState(Enum):\n    PENDING = auto()\n    APPROVED = auto()\n    DENIED = auto()\n    EXPIRED = auto()\n    REVOKED = auto()\n\n\n# ---------------------------------------------------------------------------\n# Data classes\n# ---------------------------------------------------------------------------\n\n\n@dataclass\nclass LeaseRequest:\n    """A request to perform a gate-controlled operation."""\n\n    operation_type: OperationType\n    description: str\n    requested_by: str  # Agent ID or component name\n    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))\n    metadata: dict[str, Any] = field(default_factory=dict)\n\n    def __post_init__(self) -> None:\n        if self.operation_type not in VALID_OPERATIONS:\n            raise ValueError(\n                f"operation_type {self.operation_type!r} is not valid. "\n                f"Choose from: {sorted(VALID_OPERATIONS)}"\n            )\n        if not self.description.strip():\n            raise ValueError("LeaseRequest.description must not be empty")\n        if not self.requested_by.strip():\n            raise ValueError("LeaseRequest.requested_by must not be empty")\n\n\n@dataclass\nclass LeaseToken:\n    """Represents the approval state of a LeaseRequest."""\n\n    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))\n    request: LeaseRequest | None = None\n    approved: bool = False\n    denied: bool = False\n    approved_at: datetime | None = None\n    denied_at: datetime | None = None\n    expires_at: datetime | None = None\n    revoked: bool = False\n    revoke_reason: str = ""\n    _state: LeaseState = field(default=LeaseState.PENDING, init=False, repr=False)\n\n    def is_valid(self) -> bool:\n        """True if the token is approved, not expired, and not revoked."""\n        if not self.approved or self.denied or self.revoked:\n            return False\n        if self.expires_at and datetime.now(UTC) > self.expires_at:\n            return False\n        return True\n\n    def is_expired(self) -> bool:\n        """True if the token has passed its expiry time."""\n        return bool(self.expires_at and datetime.now(UTC) > self.expires_at)\n\n    def state(self) -> LeaseState:\n        if self.revoked:\n            return LeaseState.REVOKED\n        if self.denied:\n            return LeaseState.DENIED\n        if self.is_expired():\n            return LeaseState.EXPIRED\n        if self.approved:\n            return LeaseState.APPROVED\n        return LeaseState.PENDING\n\n\n@dataclass\nclass StatusReport:\n    """Health report for a single application component or service."""\n\n    component: str\n    status: StatusLevel\n    detail: str = ""\n    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))\n    metrics: dict[str, Any] = field(default_factory=dict)\n\n    def to_dict(self) -> dict[str, Any]:\n        return {\n            "component": self.component,\n            "status": self.status.value,\n            "detail": self.detail,\n            "timestamp": self.timestamp.isoformat(),\n            "metrics": self.metrics,\n        }\n\n    def to_json(self) -> str:\n        return json.dumps(self.to_dict())\n\n\n# ---------------------------------------------------------------------------\n# LeaseGateway\n# ---------------------------------------------------------------------------\n\n\nclass LeaseGateway:\n    """\n    Stores and manages pending leases.\n\n    Workflow\n    --------\n    1. Agent calls request_lease(op) → gets a LeaseToken in PENDING state.\n    2. The LeaseToken.token_id is surfaced to the user (via UI / WebSocket).\n    3. User approves via approve_lease(token_id) or denies via deny_lease().\n    4. Agent calls await_approval(token) → blocks until approved, denied, or\n       timeout.\n    5. If approved, the guarded operation proceeds.  Otherwise it is aborted.\n\n    Thread Safety\n    -------------\n    All state mutations are protected by an internal threading.Lock, making\n    the gateway safe for concurrent use from multiple agent threads.\n    """\n\n    def __init__(\n        self,\n        default_ttl_seconds: int = 300,\n        approval_callback: Callable[[LeaseToken], None] | None = None,\n    ) -> None:\n        self._ttl = default_ttl_seconds\n        self._approval_callback = approval_callback\n        self._leases: dict[str, LeaseToken] = {}\n        self._lock = threading.Lock()\n        self._approval_events: dict[str, threading.Event] = {}\n\n    # ------------------------------------------------------------------\n    # Public API\n    # ------------------------------------------------------------------\n\n    def request_lease(\n        self,\n        op: LeaseRequest,\n        ttl_seconds: int | None = None,\n    ) -> LeaseToken:\n        """\n        Register a new LeaseRequest and return a pending LeaseToken.\n\n        The token starts in PENDING state.  The caller must then call\n        await_approval() to wait for user sign-off.\n\n        Parameters\n        ----------\n        op:\n            The LeaseRequest describing the guarded operation.\n        ttl_seconds:\n            Override the gateway's default TTL for this lease.\n\n        Returns\n        -------\n        LeaseToken in PENDING state.\n        """\n        ttl = ttl_seconds if ttl_seconds is not None else self._ttl\n        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)\n\n        token = LeaseToken(\n            request=op,\n            approved=False,\n            denied=False,\n            expires_at=expires_at,\n        )\n\n        with self._lock:\n            self._leases[token.token_id] = token\n            evt = threading.Event()\n            self._approval_events[token.token_id] = evt\n\n        log.info(\n            "LeaseGateway: lease %s requested [%s] by %s — expires %s",\n            token.token_id[:8],\n            op.operation_type,\n            op.requested_by,\n            expires_at.isoformat(),\n        )\n\n        # Notify callback (e.g. emit to WebSocket for UI display)\n        if self._approval_callback:\n            try:\n                self._approval_callback(token)\n            except Exception as exc:\n                log.warning("approval_callback raised: %s", exc)\n\n        return token\n\n    def approve_lease(self, token_id: str, approved_by: str = "user") -> bool:\n        """\n        Mark the lease identified by *token_id* as approved.\n\n        Returns False if the token is not found, already decided, or expired.\n        """\n        with self._lock:\n            token = self._leases.get(token_id)\n            if not token:\n                log.warning("approve_lease: token %r not found", token_id)\n                return False\n            if token.denied or token.revoked:\n                log.warning("approve_lease: token %r is already decided", token_id)\n                return False\n            if token.is_expired():\n                log.warning("approve_lease: token %r has expired", token_id)\n                return False\n\n            token.approved = True\n            token.approved_at = datetime.now(UTC)\n            evt = self._approval_events.get(token_id)\n\n        log.info(\n            "LeaseGateway: lease %s APPROVED by %s",\n            token_id[:8], approved_by,\n        )\n        if evt:\n            evt.set()\n        return True\n\n    def deny_lease(self, token_id: str, reason: str = "") -> bool:\n        """\n        Mark the lease identified by *token_id* as denied.\n        """\n        with self._lock:\n            token = self._leases.get(token_id)\n            if not token:\n                log.warning("deny_lease: token %r not found", token_id)\n                return False\n            if token.approved or token.revoked:\n                log.warning("deny_lease: token %r already decided", token_id)\n                return False\n\n            token.denied = True\n            token.denied_at = datetime.now(UTC)\n            if reason:\n                token.revoke_reason = reason\n            evt = self._approval_events.get(token_id)\n\n        log.info(\n            "LeaseGateway: lease %s DENIED — %s",\n            token_id[:8], reason or "no reason given",\n        )\n        if evt:\n            evt.set()\n        return True\n\n    def revoke_lease(self, token_id: str, reason: str = "") -> bool:\n        """Revoke a previously-approved lease."""\n        with self._lock:\n            token = self._leases.get(token_id)\n            if not token:\n                return False\n            token.revoked = True\n            token.revoke_reason = reason\n            evt = self._approval_events.get(token_id)\n\n        log.info("LeaseGateway: lease %s REVOKED — %s", token_id[:8], reason)\n        if evt:\n            evt.set()\n        return True\n\n    def await_approval(\n        self,\n        lease_token: LeaseToken,\n        timeout_seconds: int = 60,\n    ) -> bool:\n        """\n        Block until the lease is approved, denied, expired, or *timeout* elapses.\n\n        Parameters\n        ----------\n        lease_token:\n            The LeaseToken returned by request_lease().\n        timeout_seconds:\n            Maximum time to wait.  Note this is a soft timeout in addition to\n            the token's own expires_at; whichever comes first wins.\n\n        Returns\n        -------\n        bool\n            True if the lease was approved and is still valid, False otherwise.\n        """\n        with self._lock:\n            evt = self._approval_events.get(lease_token.token_id)\n\n        if evt is None:\n            log.warning(\n                "await_approval: no event found for token %s",\n                lease_token.token_id[:8],\n            )\n            return False\n\n        # Compute effective wait: minimum of caller timeout and token TTL\n        now = datetime.now(UTC)\n        if lease_token.expires_at:\n            remaining_ttl = (lease_token.expires_at - now).total_seconds()\n            effective_timeout = min(timeout_seconds, max(0.0, remaining_ttl))\n        else:\n            effective_timeout = float(timeout_seconds)\n\n        log.debug(\n            "await_approval: waiting up to %.1fs for lease %s",\n            effective_timeout, lease_token.token_id[:8],\n        )\n        signalled = evt.wait(timeout=effective_timeout)\n\n        if not signalled:\n            log.warning(\n                "await_approval: lease %s timed out after %.1fs",\n                lease_token.token_id[:8], effective_timeout,\n            )\n\n        result = lease_token.is_valid()\n        log.info(\n            "await_approval: lease %s → %s",\n            lease_token.token_id[:8], "APPROVED" if result else "NOT APPROVED",\n        )\n        return result\n\n    def get_token(self, token_id: str) -> LeaseToken | None:\n        with self._lock:\n            return self._leases.get(token_id)\n\n    def pending_leases(self) -> list[LeaseToken]:\n        """Return all leases currently in PENDING state."""\n        with self._lock:\n            return [\n                t for t in self._leases.values()\n                if t.state() == LeaseState.PENDING\n            ]\n\n    def purge_expired(self) -> int:\n        """Remove expired leases from the registry. Returns count removed."""\n        with self._lock:\n            expired_ids = [\n                tid for tid, t in self._leases.items()\n                if t.is_expired() and t.state() == LeaseState.EXPIRED\n            ]\n            for tid in expired_ids:\n                del self._leases[tid]\n                self._approval_events.pop(tid, None)\n        log.debug("LeaseGateway: purged %d expired lease(s)", len(expired_ids))\n        return len(expired_ids)\n\n\n# ---------------------------------------------------------------------------\n# SystemStatusLog\n# ---------------------------------------------------------------------------\n\n\nclass SystemStatusLog:\n    """\n    Structured logger for application vector health status.\n\n    Maintains an in-memory ring buffer of the most recent status reports\n    per component, and exposes them for reporting / WebSocket emission.\n    """\n\n    def __init__(self, max_history: int = 200) -> None:\n        self._history: list[StatusReport] = []\n        self._max_history = max_history\n        self._latest: dict[str, StatusReport] = {}\n        self._lock = threading.Lock()\n\n    def log(self, report: StatusReport) -> None:\n        """Record a new StatusReport."""\n        with self._lock:\n            self._history.append(report)\n            if len(self._history) > self._max_history:\n                self._history.pop(0)\n            self._latest[report.component] = report\n\n        level_map = {\n            StatusLevel.COMPLETE: logging.INFO,\n            StatusLevel.PARTIAL: logging.WARNING,\n            StatusLevel.DEGRADED: logging.WARNING,\n            StatusLevel.UNVERIFIED: logging.DEBUG,\n        }\n        lvl = level_map.get(report.status, logging.INFO)\n        log.log(\n            lvl,\n            "SystemStatus [%s] %s: %s",\n            report.component, report.status.value, report.detail,\n        )\n\n    def latest(self, component: str) -> StatusReport | None:\n        """Return the most recent StatusReport for *component*."""\n        with self._lock:\n            return self._latest.get(component)\n\n    def all_latest(self) -> dict[str, StatusReport]:\n        """Return the most recent report for every component."""\n        with self._lock:\n            return dict(self._latest)\n\n    def degraded_components(self) -> list[StatusReport]:\n        """Return components currently in DEGRADED or UNVERIFIED state."""\n        with self._lock:\n            return [\n                r for r in self._latest.values()\n                if r.status in (StatusLevel.DEGRADED, StatusLevel.UNVERIFIED)\n            ]\n\n    def history(self, limit: int = 50) -> list[StatusReport]:\n        """Return the last *limit* reports."""\n        with self._lock:\n            return list(self._history[-limit:])\n\n    def summary(self) -> dict[str, Any]:\n        """Return a JSON-serialisable summary of current system health."""\n        all_reports = self.all_latest()\n        return {\n            "generated_at": datetime.now(UTC).isoformat(),\n            "component_count": len(all_reports),\n            "components": {\n                name: report.to_dict()\n                for name, report in all_reports.items()\n            },\n            "degraded_count": len(self.degraded_components()),\n        }\n\n\n# ---------------------------------------------------------------------------\n# TelemetryChannel\n# ---------------------------------------------------------------------------\n\n\nclass TelemetryChannel:\n    """\n    Emits StatusReport objects to the Electron frontend over WebSocket.\n\n    The channel maintains a non-blocking outbound queue and uses a background\n    daemon thread to drain it.  If no WebSocket clients are connected, emits\n    are silently dropped (no backpressure into the agent loop).\n\n    Parameters\n    ----------\n    host:\n        WebSocket server host.  Defaults to 'localhost'.\n    port:\n... (file truncated, showing first 500 lines) ...
```

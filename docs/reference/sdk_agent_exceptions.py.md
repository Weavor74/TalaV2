# File: `exceptions.py`

## Module Description

exceptions.py — Tala Cognitive Engine Room: Agent SDK Custom Exceptions
=======================================================================
All domain-specific exceptions raised by the Tala Agent SDK.  Every
exception carries a human-readable message AND a structured payload so
that callers can react programmatically without string-parsing.

Hierarchy
---------
TalaBaseError
  ├── InvalidNodeSchemaError        (parser / schema enforcement)
  ├── AuthorityBoundaryViolation    (activation / governance)
  ├── Mem0WriteBlockedError         (memory / write-guard)
  ├── InsufficientTokenBalanceError (economic layer)
  ├── InsufficientEscrowError       (economic layer)
  ├── PiiAccessBlockedError         (privacy / compliance)
  └── StyleCapExceededError         (style / persona governance)

## Classes

### `class TalaBaseError`
Root exception for all Tala Agent SDK errors.

Attributes
----------
message : str
    Human-readable error description.
payload : dict[str, Any]
    Structured machine-readable context (varies by subclass).

- **`def __init__(self, message)`**
- **`def __repr__(self)`**

### `class InvalidNodeSchemaError`
Raised when an HTML Neural Node fails schema validation.

This exception is non-recoverable for the document that triggered
it.  The caller must fix the source HTML and re-parse.

Payload keys
------------
path : str | None
    Filesystem path of the offending file (if known).
missing_attributes : list[str]
    List of required HTML attributes that were absent.
offending_html : str | None
    Snippet of the HTML that failed validation (truncated to 512 chars).

- **`def __init__(self, message)`**

### `class AuthorityBoundaryViolation`
Raised when activation logic attempts to mutate a node's
``execution_authority`` field.

The Authority Boundary Law states: *execution_authority is immutable
once set at parse-time*.  No activation score, propagation, or
external signal may alter it.  Violations are hard errors, not
warnings, because silent authority inflation is a security risk.

Payload keys
------------
node_id : str
    The node whose authority was targeted.
attempted_value : float
    The value that would have been written.
current_authority : float
    The node's actual, immutable authority value.

- **`def __init__(self, message)`**

### `class Mem0WriteBlockedError`
Raised when a write to the Mem0 persistent memory layer is blocked.

Writes may be blocked because:
- The node lacks sufficient ``execution_authority``.
- A write-lock is held by another process.
- The memory quota for the agent session has been reached.

Payload keys
------------
node_id : str
    Node that attempted the write.
reason : str
    Human-readable block reason ('authority', 'lock', 'quota').
required_authority : float | None
    Minimum authority threshold required for this write (if applicable).

- **`def __init__(self, message)`**

### `class InsufficientTokenBalanceError`
Raised when an agent operation requires more tokens than are
currently available in the session balance.

Payload keys
------------
required : int
    Tokens required for the operation.
available : int
    Tokens currently available.
operation : str
    Description of the operation that triggered the check.

- **`def __init__(self, message)`**

### `class InsufficientEscrowError`
Raised when an inter-agent or cross-boundary operation cannot be
funded because the escrow account holds less than the required
amount.

Payload keys
------------
required : float
    Amount required to be held in escrow.
escrowed : float
    Amount currently in escrow.
transaction_id : str | None
    The transaction reference that triggered the check.

- **`def __init__(self, message)`**

### `class PiiAccessBlockedError`
Raised when a node or pipeline attempts to read, write, or emit data
that has been classified as Personally Identifiable Information (PII)
without the appropriate access clearance.

Payload keys
------------
data_field : str
    The field or key that was flagged as PII.
node_id : str | None
    The node that attempted access (if known).
classification : str
    PII classification label (e.g. 'name', 'email', 'ssn').

- **`def __init__(self, message)`**

### `class StyleCapExceededError`
Raised when a node's output exceeds the configured style or persona
cap — for example, when a response exceeds the maximum permitted
formality deviation, verbosity budget, or persona drift threshold.

Payload keys
------------
metric : str
    The style metric that was exceeded (e.g. 'verbosity', 'formality').
cap : float
    The configured cap value.
actual : float
    The actual value that triggered the violation.
node_id : str | None
    The generating node (if known).

- **`def __init__(self, message)`**

### `class SafetyGuardrailViolation`
Raised when input prompts or model outputs violate configured safety guardrails.

Payload keys
------------
rule_violated : str
    The specific guardrail rule that was violated.
trigger_value : str
    The value or pattern that triggered the violation (truncated to 512 chars).

- **`def __init__(self, message)`**

## Source Code

```python
"""
exceptions.py — Tala Cognitive Engine Room: Agent SDK Custom Exceptions
=======================================================================
All domain-specific exceptions raised by the Tala Agent SDK.  Every
exception carries a human-readable message AND a structured payload so
that callers can react programmatically without string-parsing.

Hierarchy
---------
TalaBaseError
  ├── InvalidNodeSchemaError        (parser / schema enforcement)
  ├── AuthorityBoundaryViolation    (activation / governance)
  ├── Mem0WriteBlockedError         (memory / write-guard)
  ├── InsufficientTokenBalanceError (economic layer)
  ├── InsufficientEscrowError       (economic layer)
  ├── PiiAccessBlockedError         (privacy / compliance)
  └── StyleCapExceededError         (style / persona governance)
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class TalaBaseError(Exception):
    """
    Root exception for all Tala Agent SDK errors.

    Attributes
    ----------
    message : str
        Human-readable error description.
    payload : dict[str, Any]
        Structured machine-readable context (varies by subclass).
    """

    def __init__(self, message: str, **payload: Any) -> None:
        super().__init__(message)
        self.message: str = message
        self.payload: dict[str, Any] = payload

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.message!r}, payload={self.payload!r})"


# ---------------------------------------------------------------------------
# Parser / Schema
# ---------------------------------------------------------------------------

class InvalidNodeSchemaError(TalaBaseError):
    """
    Raised when an HTML Neural Node fails schema validation.

    This exception is non-recoverable for the document that triggered
    it.  The caller must fix the source HTML and re-parse.

    Payload keys
    ------------
    path : str | None
        Filesystem path of the offending file (if known).
    missing_attributes : list[str]
        List of required HTML attributes that were absent.
    offending_html : str | None
        Snippet of the HTML that failed validation (truncated to 512 chars).
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        missing_attributes: list[str] | None = None,
        offending_html: str | None = None,
    ) -> None:
        super().__init__(
            message,
            path=path,
            missing_attributes=missing_attributes or [],
            offending_html=(offending_html or "")[:512],
        )


# ---------------------------------------------------------------------------
# Activation / Governance
# ---------------------------------------------------------------------------

class AuthorityBoundaryViolation(TalaBaseError):
    """
    Raised when activation logic attempts to mutate a node's
    ``execution_authority`` field.

    The Authority Boundary Law states: *execution_authority is immutable
    once set at parse-time*.  No activation score, propagation, or
    external signal may alter it.  Violations are hard errors, not
    warnings, because silent authority inflation is a security risk.

    Payload keys
    ------------
    node_id : str
        The node whose authority was targeted.
    attempted_value : float
        The value that would have been written.
    current_authority : float
        The node's actual, immutable authority value.
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        attempted_value: float,
        current_authority: float,
    ) -> None:
        super().__init__(
            message,
            node_id=node_id,
            attempted_value=attempted_value,
            current_authority=current_authority,
        )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class Mem0WriteBlockedError(TalaBaseError):
    """
    Raised when a write to the Mem0 persistent memory layer is blocked.

    Writes may be blocked because:
    - The node lacks sufficient ``execution_authority``.
    - A write-lock is held by another process.
    - The memory quota for the agent session has been reached.

    Payload keys
    ------------
    node_id : str
        Node that attempted the write.
    reason : str
        Human-readable block reason ('authority', 'lock', 'quota').
    required_authority : float | None
        Minimum authority threshold required for this write (if applicable).
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        reason: str,
        required_authority: float | None = None,
    ) -> None:
        super().__init__(
            message,
            node_id=node_id,
            reason=reason,
            required_authority=required_authority,
        )


# ---------------------------------------------------------------------------
# Economic layer
# ---------------------------------------------------------------------------

class InsufficientTokenBalanceError(TalaBaseError):
    """
    Raised when an agent operation requires more tokens than are
    currently available in the session balance.

    Payload keys
    ------------
    required : int
        Tokens required for the operation.
    available : int
        Tokens currently available.
    operation : str
        Description of the operation that triggered the check.
    """

    def __init__(
        self,
        message: str,
        *,
        required: int,
        available: int,
        operation: str,
    ) -> None:
        super().__init__(
            message,
            required=required,
            available=available,
            operation=operation,
        )


class InsufficientEscrowError(TalaBaseError):
    """
    Raised when an inter-agent or cross-boundary operation cannot be
    funded because the escrow account holds less than the required
    amount.

    Payload keys
    ------------
    required : float
        Amount required to be held in escrow.
    escrowed : float
        Amount currently in escrow.
    transaction_id : str | None
        The transaction reference that triggered the check.
    """

    def __init__(
        self,
        message: str,
        *,
        required: float,
        escrowed: float,
        transaction_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            required=required,
            escrowed=escrowed,
            transaction_id=transaction_id,
        )


# ---------------------------------------------------------------------------
# Privacy / Compliance
# ---------------------------------------------------------------------------

class PiiAccessBlockedError(TalaBaseError):
    """
    Raised when a node or pipeline attempts to read, write, or emit data
    that has been classified as Personally Identifiable Information (PII)
    without the appropriate access clearance.

    Payload keys
    ------------
    data_field : str
        The field or key that was flagged as PII.
    node_id : str | None
        The node that attempted access (if known).
    classification : str
        PII classification label (e.g. 'name', 'email', 'ssn').
    """

    def __init__(
        self,
        message: str,
        *,
        data_field: str,
        node_id: str | None = None,
        classification: str = "unknown",
    ) -> None:
        super().__init__(
            message,
            data_field=data_field,
            node_id=node_id,
            classification=classification,
        )


# ---------------------------------------------------------------------------
# Style / Persona governance
# ---------------------------------------------------------------------------

class StyleCapExceededError(TalaBaseError):
    """
    Raised when a node's output exceeds the configured style or persona
    cap — for example, when a response exceeds the maximum permitted
    formality deviation, verbosity budget, or persona drift threshold.

    Payload keys
    ------------
    metric : str
        The style metric that was exceeded (e.g. 'verbosity', 'formality').
    cap : float
        The configured cap value.
    actual : float
        The actual value that triggered the violation.
    node_id : str | None
        The generating node (if known).
    """

    def __init__(
        self,
        message: str,
        *,
        metric: str,
        cap: float,
        actual: float,
        node_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            metric=metric,
            cap=cap,
            actual=actual,
            node_id=node_id,
        )


class SafetyGuardrailViolation(TalaBaseError):
    """
    Raised when input prompts or model outputs violate configured safety guardrails.

    Payload keys
    ------------
    rule_violated : str
        The specific guardrail rule that was violated.
    trigger_value : str
        The value or pattern that triggered the violation (truncated to 512 chars).
    """

    def __init__(self, message: str, *, rule_violated: str, trigger_value: str) -> None:
        super().__init__(
            message,
            rule_violated=rule_violated,
            trigger_value=trigger_value[:512],
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TalaBaseError",
    "InvalidNodeSchemaError",
    "AuthorityBoundaryViolation",
    "Mem0WriteBlockedError",
    "InsufficientTokenBalanceError",
    "InsufficientEscrowError",
    "PiiAccessBlockedError",
    "StyleCapExceededError",
    "SafetyGuardrailViolation",
]

```

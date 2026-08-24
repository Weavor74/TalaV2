"""
ui_bridge.py — ask the renderer a question and wait for the answer.

``broadcast_notification`` is fire-and-forget. That is fine for "navigate to
this view", but it is the reason ``browser.get_dom`` and ``browser.execute_js``
were stubs that reported success (UI-014, UI-015): there was no way for the
renderer to hand anything back, so the handlers broadcast into the void and
returned ``{"status": "broadcast_sent"}``.

This adds the missing half. A request carries a ``request_id``; the renderer
does the work and calls the ``ui.bridge_result`` RPC with that id; the pending
future resolves. Three properties matter and each one closes a failure the
audit found:

* **No listener is an error, not a success.** ``broadcast_notification``
  returns a delivery count, so zero clients fails immediately rather than
  waiting out the timeout and then lying.
* **Every request is bounded.** A renderer that never answers -- crashed,
  navigated away, no handler for that channel -- times out with a real error
  instead of hanging the harness.
* **Failures arrive as failures.** If the JS throws, the renderer sends the
  error back and the caller gets ``_err``, not an empty string that looks like
  an empty page.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: request_id -> the future awaiting that renderer response.
_pending: dict[str, asyncio.Future] = {}

DEFAULT_TIMEOUT = 8.0


class BridgeError(RuntimeError):
    """Raised when the renderer could not, or did not, answer."""


class NoListener(BridgeError):
    """Nothing was connected to receive the request."""


async def request(
    channel: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """
    Broadcast ``channel`` and wait for a renderer to answer.

    Returns whatever the renderer sent as ``result``. Raises :class:`NoListener`
    if no client is connected, :class:`BridgeError` on renderer-side failure,
    and :class:`asyncio.TimeoutError` if nobody answers in time.
    """
    from python.harness.server import broadcast_notification

    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending[request_id] = future

    try:
        body = dict(payload or {})
        body["request_id"] = request_id
        reached = await broadcast_notification(channel, body)

        if not reached:
            raise NoListener(
                f"No UI client is connected, so {channel!r} could not be delivered. "
                "Open the Tala window, or use the headless browser tools instead."
            )

        return await asyncio.wait_for(future, timeout=timeout)

    except asyncio.TimeoutError:
        log.warning(
            "ui_bridge: %s (%s) went unanswered after %.1fs. The renderer may have "
            "no handler for this channel.", channel, request_id, timeout,
        )
        raise
    finally:
        _pending.pop(request_id, None)


def resolve(request_id: str, result: Any = None, error: str | None = None) -> bool:
    """
    Deliver a renderer response. Returns False if nothing was waiting for it.

    A late answer -- one that arrives after its request timed out -- is dropped
    rather than raising, because by then the caller has already been told the
    truth and is gone.
    """
    future = _pending.get(request_id)
    if future is None or future.done():
        return False
    if error:
        future.set_exception(BridgeError(error))
    else:
        future.set_result(result)
    return True


def pending_count() -> int:
    """How many requests are currently in flight. Used by diagnostics."""
    return sum(1 for f in _pending.values() if not f.done())

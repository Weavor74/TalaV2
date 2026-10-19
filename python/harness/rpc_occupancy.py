"""
python.harness.rpc_occupancy — keep status RPCs answering during inference.

NAME
       rpc_occupancy - long-job occupancy and a non-blocking RPC lane

SYNOPSIS
       from python.harness.rpc_occupancy import occupancy, run_rpc_handler
       snapshot = occupancy.snapshot()
       response = await run_rpc_handler(method, handler_coro)

DESCRIPTION
       Chat / inference can run for minutes on CPU. Status, catalogue and
       agent-list methods must still answer inside their renderer timeout.
       Long methods are marked busy and scheduled so the event loop can run
       fast methods concurrently. Blocking CPU work belongs in
       ``asyncio.to_thread`` (see ``run_blocking``), never on the dispatcher
       coroutine itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

FAST_STATUS_METHODS = frozenset({
    "gateway.status",
    "tools.catalogue",
    "agent.get_agents",
    "harness.health",
    "harness.ws_status",
    "inference.get_embedded_status",
    "inference.get_providers",
})

LONG_RPC_METHODS = frozenset({
    "inference.generate",
    "workflow.execute",
    "crew.run",
    "desktop.run",
    "task.submit",
    "research.crawl_and_summarize",
    "self_improvement.execute_goal",
    "self_improvement.execute_plan",
    "code.generate",
    "code.repair",
    "telemetry.stream_subscribe",
})


class RpcOccupancy:
    """
    NAME
           RpcOccupancy - in-process count of long-running RPC methods

    SYNOPSIS
           occupancy.enter_long(method)
           occupancy.exit_long(method)
           occupancy.snapshot() -> dict

    DESCRIPTION
           Fast status handlers read this without waiting on the long job.
           ``busy`` is true while any long method is in flight.
    """

    def __init__(self) -> None:
        self._inflight: list[str] = []

    def enter_long(self, method: str) -> None:
        self._inflight.append(method)

    def exit_long(self, method: str) -> None:
        try:
            self._inflight.remove(method)
        except ValueError:
            pass

    @property
    def busy(self) -> bool:
        return bool(self._inflight)

    def snapshot(self) -> dict[str, Any]:
        methods = list(self._inflight)
        current = methods[-1] if methods else ""
        return {
            "busy": bool(methods),
            "busy_method": current,
            "busy_detail": f"Tala is busy ({current})" if current else "",
            "inflight": methods,
        }


occupancy = RpcOccupancy()


@asynccontextmanager
async def long_rpc(method: str):
    """
    NAME
           long_rpc - mark a long RPC busy and yield the event loop once

    SYNOPSIS
           async with long_rpc("inference.generate"):
               await generate()

    DESCRIPTION
           Entering yields once so a status RPC that arrived in the same
           tick can start before the long job's first blocking stretch.
    """
    occupancy.enter_long(method)
    try:
        await asyncio.sleep(0)
        yield
    finally:
        occupancy.exit_long(method)


async def run_rpc_handler(
    method: str,
    handler: Callable[[], Awaitable[T]],
) -> T:
    """
    NAME
           run_rpc_handler - run a handler, tracking long jobs

    SYNOPSIS
           result = await run_rpc_handler(method, lambda: handler(params, req_id))

    DESCRIPTION
           Long methods take the occupancy lane. Fast status methods run as
           soon as the event loop can schedule them, even while a long job
           is awaiting inference I/O.
    """
    if method in LONG_RPC_METHODS:
        async with long_rpc(method):
            return await handler()
    return await handler()


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    NAME
           run_blocking - run a sync function off the RPC event loop

    SYNOPSIS
           result = await run_blocking(slow_fn, arg)

    DESCRIPTION
           Inference or catalogue parsing that holds the GIL or the CPU
           must not sit on the dispatcher coroutine. ``asyncio.to_thread``
           is the cross-platform adapter.
    """
    return await asyncio.to_thread(func, *args, **kwargs)

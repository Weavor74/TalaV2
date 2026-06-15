"""
sdk/agent/telemetry.py
======================
Live Telemetry & WebSocket Log Streaming — Tala Cognitive Engine Room
Milestone 7 | Task 7.1

Implements:
  • LiveStatusReporter — Formats and broadcasts current phase and details to client UI.
  • LogStreamer        — Buffers stdout/stderr streams to be picked up by WebSockets.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Registry of active WebSocket connection objects to stream to
_ACTIVE_WEBSOCKETS: list[Any] = []


class LiveStatusReporter:
    """
    Streams live task FSM state and loop phase alerts to listening Electron clients.
    """

    @classmethod
    def register_websocket(cls, ws: Any) -> None:
        """Register a client websocket connection."""
        _ACTIVE_WEBSOCKETS.append(ws)

    @classmethod
    def unregister_websocket(cls, ws: Any) -> None:
        """Unregister a client websocket connection."""
        if ws in _ACTIVE_WEBSOCKETS:
            _ACTIVE_WEBSOCKETS.remove(ws)

    @classmethod
    async def broadcast_status(
        cls,
        task_id: str,
        phase: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None
    ) -> None:
        """
        Send a real-time status update to all registered websockets.
        """
        payload = {
            "type": "task_status_update",
            "task_id": task_id,
            "phase": phase,
            "status": status,
            "message": message,
            "details": details or {}
        }
        
        log.info("LiveStatusReporter: [%s] phase=%s: %s", task_id[:8], phase, message)
        
        # In a real environment, we loop over ws and call send()
        for ws in _ACTIVE_WEBSOCKETS:
            try:
                # Expect ws has an async send() method matching websockets API
                await ws.send(json.dumps(payload))
            except Exception as e:
                log.warning("LiveStatusReporter: failed broadcasting to websocket: %s", e)


import asyncio
from sdk.agent.persistence import TaskEventStore

class LogStreamer:
    """
    Buffers running shell outputs and log records to be streamed to UI terminal windows.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.buffer: list[str] = []
        self._current_phase_stdout: list[str] = []
        self._current_phase_stderr: list[str] = []

    def log_stdout(self, data: str) -> None:
        """Buffer stdout and trigger log events."""
        clean_data = data.strip()
        if clean_data:
            self.buffer.append(f"[STDOUT] {clean_data}")
            self._current_phase_stdout.append(clean_data)
            log.debug("LogStreamer: task %s: %s", self.task_id[:8], clean_data)

    def log_stderr(self, data: str) -> None:
        """Buffer stderr and trigger log events."""
        clean_data = data.strip()
        if clean_data:
            self.buffer.append(f"[STDERR] {clean_data}")
            self._current_phase_stderr.append(clean_data)
            log.warning("LogStreamer: task %s ERR: %s", self.task_id[:8], clean_data)

    def flush_phase_summary(
        self,
        phase_name: str,
        command_invoked: str,
        validator_result: bool,
        final_status: str,
        artifacts_touched: list[str],
        debug_mode: bool = False
    ) -> None:
        """Flushes a compact bundle to the Event Store for the completed phase."""
        stdout_summary = self._current_phase_stdout[-10:] if not debug_mode else self._current_phase_stdout
        stderr_summary = self._current_phase_stderr[-10:] if not debug_mode else self._current_phase_stderr
        
        payload = {
            "phase": phase_name,
            "status": final_status,
            "command": command_invoked,
            "validator_result": validator_result,
            "stdout_summary": stdout_summary,
            "stderr_summary": stderr_summary,
            "artifacts_touched": artifacts_touched,
        }
        
        # Reset current phase buffers
        self._current_phase_stdout.clear()
        self._current_phase_stderr.clear()
        
        try:
            asyncio.create_task(TaskEventStore.append_event(self.task_id, "phase_complete", payload))
        except RuntimeError:
            pass

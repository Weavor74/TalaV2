"""
sdk/agent/persistence.py
========================
Event-Sourcing, Projection, & Telemetry Layer — Tala Cognitive Engine Room
Milestone 1 | Tasks 1.5, 1.6, 1.7, 1.8

Implements:
  • TaskEventStore — Append-only store for FSM transitions and tool invocations.
  • project_task_state — Replays event streams to reconstruct canonical task state.
  • generate_deterministic_id — UUIDv5 hashing for reproducible task IDs.
  • log_telemetry_event — Structured telemetry generation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from python.harness import db

log = logging.getLogger(__name__)

# Namespace for deterministic UUIDv5 generation
TALA_NAMESPACE = uuid.UUID("3c9b68a6-8c43-4e89-8d7b-eb39e08396a7")

# In-memory store for fallback/testing when database pool is not initialized
_in_memory_event_store: dict[str, list[dict[str, Any]]] = {}


def generate_deterministic_id(payload: dict[str, Any], salt: str = "") -> str:
    """
    Generate a deterministic UUIDv5 based on task input vectors.
    
    Ensures duplicate submissions generate identical task identifiers.
    """
    # Normalize payload keys and convert to serialized representation
    serialized = json.dumps(payload, sort_keys=True)
    hash_input = f"{serialized}:{salt}"
    return str(uuid.uuid5(TALA_NAMESPACE, hash_input))


class TaskEventStore:
    """
    Append-only persistent event log for task execution.
    
    If the PostgreSQL connection pool is not initialized, it falls back
    gracefully to an in-memory dictionary for sandboxed testing.
    """

    @classmethod
    async def append_event(
        cls,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """
        Append a new event to the task event store.
        
        Returns
        -------
        int
            The event ID / sequence number.
        """
        # Ensure payload is JSON-serializable
        sanitized_payload = cls._sanitize_payload(payload)
        
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            # DB pool not initialized — fall back to in-memory store
            pass

        if db_available:
            try:
                # Append to Postgres task_event_store table
                query = """
                    INSERT INTO task_event_store (task_id, event_type, payload)
                    VALUES ($1, $2, $3::jsonb)
                    RETURNING id
                """
                payload_str = json.dumps(sanitized_payload)
                event_id = await db.fetchval(query, task_id, event_type, payload_str)
                log.debug("Event appended to DB for task %s (type: %s, ID: %s)", task_id, event_type, event_id)

                # Broadcast live status update notification to connected UI clients
                try:
                    from python.harness.server import broadcast_notification
                    await broadcast_notification("task.event", {
                        "task_id": task_id,
                        "event_type": event_type,
                        "payload": sanitized_payload,
                        "created_at": datetime.now(UTC).isoformat()
                    })
                except Exception:
                    pass

                return int(event_id)
            except Exception as exc:
                log.error("Failed to append event to database, writing to memory fallback: %s", exc)

        # Fallback to in-memory store
        if task_id not in _in_memory_event_store:
            _in_memory_event_store[task_id] = []
        
        event_record = {
            "id": len(_in_memory_event_store[task_id]) + 1,
            "task_id": task_id,
            "event_type": event_type,
            "payload": sanitized_payload,
            "created_at": datetime.now(UTC),
        }
        _in_memory_event_store[task_id].append(event_record)
        log.debug("Event appended to memory for task %s (type: %s, ID: %s)", task_id, event_type, event_record["id"])

        # Broadcast live status update notification to connected UI clients
        try:
            from python.harness.server import broadcast_notification
            await broadcast_notification("task.event", {
                "task_id": task_id,
                "event_type": event_type,
                "payload": sanitized_payload,
                "created_at": datetime.now(UTC).isoformat()
            })
        except Exception:
            pass

        return event_record["id"]

    @classmethod
    async def fetch_events(cls, task_id: str) -> list[dict[str, Any]]:
        """Fetch all events for a given task, sorted chronologically."""
        db_available = False
        try:
            db.get_pool()
            db_available = True
        except RuntimeError:
            pass

        if db_available:
            try:
                query = """
                    SELECT id, task_id, event_type, payload, created_at
                    FROM task_event_store
                    WHERE task_id = $1
                    ORDER BY id ASC
                """
                rows = await db.fetch(query, task_id)
                events = []
                for r in rows:
                    event_dict = dict(r)
                    payload = event_dict.get("payload")
                    if isinstance(payload, str):
                        try:
                            event_dict["payload"] = json.loads(payload)
                        except Exception as parse_exc:
                            log.warning("Failed to deserialize event payload: %s", parse_exc)
                    events.append(event_dict)
                return events
            except Exception as exc:
                log.error("Failed to fetch events from database: %s", exc)

        # Fallback to in-memory store
        return _in_memory_event_store.get(task_id, [])

    @staticmethod
    def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Convert non-serializable objects (like datetimes) to strings."""
        def serialize_item(val: Any) -> Any:
            if isinstance(val, (datetime, uuid.UUID)):
                return str(val)
            if isinstance(val, dict):
                return {k: serialize_item(v) for k, v in val.items()}
            if isinstance(val, (list, tuple)):
                return [serialize_item(item) for item in val]
            return val
        return serialize_item(payload)


def project_task_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Replay a stream of task events to reconstruct the canonical task state.
    
    Replays transitions, tool executions, and iterations.
    """
    state = {
        "task_id": None,
        "status": "pending",
        "description": "",
        "iterations": 0,
        "steps_executed": [],
        "phases_completed": [],
        "logs": [],
        "artifacts": [],
        "facts": [],
        "anomalies": [],
        "telemetry": [],
        "completed_at": None,
        "failed_reason": None,
    }

    for event in events:
        event_type = event["event_type"]
        payload = event["payload"]
        
        if not state["task_id"]:
            state["task_id"] = event["task_id"]
            
        if event_type == "state_transition":
            state["status"] = payload.get("to_status", state["status"])
            if state["status"] == "complete":
                state["completed_at"] = payload.get("timestamp", datetime.now(UTC).isoformat())
            elif state["status"] == "failed":
                state["failed_reason"] = payload.get("reason", "Unknown failure")

        elif event_type == "task_init":
            state["description"] = payload.get("description", "")
            
        elif event_type == "iteration_start":
            state["iterations"] = payload.get("iteration", state["iterations"])
            
        elif event_type == "step_execution":
            step = payload.get("step")
            if step:
                state["steps_executed"].append(step)

        elif event_type == "observation":
            state["facts"].extend(payload.get("facts", []))
            state["anomalies"].extend(payload.get("anomalies", []))

        elif event_type == "telemetry":
            state["telemetry"].append(payload)

        elif event_type == "phase_complete":
            state["phases_completed"].append(payload)

        elif event_type == "execution_log":
            state["logs"].append(payload)

        elif event_type == "artifact_generated":
            state["artifacts"].append(payload)

    return state


async def log_telemetry_event(
    task_id: str,
    metric_name: str,
    metric_value: Any,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Format and write a telemetry event into the Event Store."""
    payload = {
        "metric_name": metric_name,
        "metric_value": metric_value,
        "timestamp": datetime.now(UTC).isoformat(),
        "metadata": metadata or {},
    }
    return await TaskEventStore.append_event(task_id, "telemetry", payload)

# File: `status_api.py`

## Module Description

sdk/agent/status_api.py
=======================
Agent Status API — Tala Cognitive Engine Room

Provides a user-facing API to query the state and outcome of an agent's task.
Lets users ask “did you complete it?” and get real task state, logs, phase results, and final artifacts.

## Classes

### `class AgentStatusAPI`
User-facing API to query the state and outcome of an agent's task.

- **`def get_task_status(cls, task_id)`**
  - Query the complete historical and current state of a task by its ID.
  
  Returns a dictionary containing:
  - state: The reconstructed FSM state (pending, in_progress, complete, failed)
  - phases: List of completed phases and their outputs
  - logs: Full execution logs (stdout/stderr)
  - artifacts: Paths/URIs to generated artifacts or skill documents

## Source Code

```python
"""
sdk/agent/status_api.py
=======================
Agent Status API — Tala Cognitive Engine Room

Provides a user-facing API to query the state and outcome of an agent's task.
Lets users ask “did you complete it?” and get real task state, logs, phase results, and final artifacts.
"""

from __future__ import annotations

from typing import Any
import logging

from sdk.agent.persistence import TaskEventStore, project_task_state

log = logging.getLogger(__name__)

class AgentStatusAPI:
    """
    User-facing API to query the state and outcome of an agent's task.
    """

    @classmethod
    async def get_task_status(cls, task_id: str) -> dict[str, Any]:
        """
        Query the complete historical and current state of a task by its ID.

        Returns a dictionary containing:
        - state: The reconstructed FSM state (pending, in_progress, complete, failed)
        - phases: List of completed phases and their outputs
        - logs: Full execution logs (stdout/stderr)
        - artifacts: Paths/URIs to generated artifacts or skill documents
        """
        log.info("AgentStatusAPI: Fetching status for task_id=%s", task_id)
        events = await TaskEventStore.fetch_events(task_id)
        
        if not events:
            log.warning("AgentStatusAPI: No events found for task_id=%s", task_id)
            return {
                "task_id": task_id,
                "status": "not_found",
                "message": "Task ID not found in event store."
            }

        state = project_task_state(events)

        # Standardized return envelope
        return {
            "task_id": state.get("task_id", task_id),
            "status": state.get("status", "unknown"),
            "description": state.get("description", ""),
            "completed_at": state.get("completed_at"),
            "failed_reason": state.get("failed_reason"),
            "state": state,
            "phases": state.get("phases_completed", []),
            "logs": state.get("logs", []),
            "artifacts": state.get("artifacts", []),
        }

```

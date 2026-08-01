# File: `sync_manager.py`

## Classes

### `class SyncManager`
Offline-first chat continuity and state synchronization service.
Writes append-only JSONL snapshots to the `.tala_snapshots` directory, 
making it safe for synchronization via Syncthing, Dropbox, or OneDrive.

- **`def __init__(self, storage_dir)`**
- **`def append_session_event(self, session_id, event_type, data)`**
  - Append a chat or execution event to the synced session log.
- **`def append_memory_event(self, key, value)`**
  - Append an update to the user model.
- **`def reconstruct_memory(self)`**
  - Rebuild the latest memory state from the append-only log.

## Source Code

```python
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class SyncManager:
    """
    Offline-first chat continuity and state synchronization service.
    Writes append-only JSONL snapshots to the `.tala_snapshots` directory, 
    making it safe for synchronization via Syncthing, Dropbox, or OneDrive.
    """
    def __init__(self, storage_dir: str = ".tala_snapshots"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.session_log = self.storage_dir / "sessions.jsonl"
        self.memory_log = self.storage_dir / "memory_model.jsonl"
        
    def append_session_event(self, session_id: str, event_type: str, data: dict):
        """Append a chat or execution event to the synced session log."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event_type": event_type,
            "data": data
        }
        try:
            with open(self.session_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\\n")
        except Exception as e:
            logger.error(f"Failed to append session event: {e}")
            
    def append_memory_event(self, key: str, value: dict):
        """Append an update to the user model."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "key": key,
            "value": value
        }
        try:
            with open(self.memory_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\\n")
        except Exception as e:
            logger.error(f"Failed to append memory event: {e}")
            
    def reconstruct_memory(self) -> dict:
        """Rebuild the latest memory state from the append-only log."""
        state = {}
        if not self.memory_log.exists():
            return state
            
        try:
            with open(self.memory_log, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        state[record["key"]] = record["value"]
                    except Exception as e:
                        logger.warning(f"Error reading memory record: {e}")
        except Exception as e:
            logger.error(f"Failed to reconstruct memory: {e}")
        return state

sync_manager = SyncManager()

```

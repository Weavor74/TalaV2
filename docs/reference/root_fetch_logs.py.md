# File: `fetch_logs.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
import asyncio
import json
from python.harness import db

async def main():
    await db.init_db()
    
    # Fetch recent logs
    logs = await db.fetch("SELECT level, message, details, created_at FROM system_logs ORDER BY created_at DESC LIMIT 20")
    for log in logs:
        if "inference.generate start" not in log["message"]:
            print(f"[{log['level']}] {log['message']}")
            if log["details"]:
                print(f"  Details: {log['details']}")

asyncio.run(main())

```

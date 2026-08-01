# File: `query_db.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
import asyncio
import asyncpg
import json

async def main():
    conn = await asyncpg.connect("postgresql://tala:tala@localhost:5432/tala")
    rows = await conn.fetch("SELECT id, name, type, config, status FROM inference_providers")
    for r in rows:
        print(dict(r))
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

```

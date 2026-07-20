"""
get_logs.py - Auto-generated documentation base.
"""

import asyncpg
import asyncio

async def main():
    conn = await asyncpg.connect('postgresql://tala:tala@localhost:5432/tala')
    rows = await conn.fetch("SELECT level, message FROM system_logs WHERE message LIKE '%inference%' OR message LIKE '%GoalEngine%' ORDER BY created_at DESC LIMIT 10")
    for r in reversed(rows):
        print(f"[{r['level']}] {r['message']}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())

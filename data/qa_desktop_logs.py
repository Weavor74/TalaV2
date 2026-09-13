"""NAME
       qa_desktop_logs - inspect recent desktop failures without changing state
DESCRIPTION
       Print bounded diagnostic events for desktop control and inference.
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from python.harness import db

async def main():
    await db.init_pool()
    rows = await db.fetch("SELECT created_at, level, message FROM agent_log WHERE message ILIKE '%desktop_%' OR message ILIKE '%429%' OR message ILIKE '%vision%' ORDER BY created_at DESC LIMIT 35")
    for row in rows:
        print(str(row['created_at']), row['level'], str(row['message'])[:1400])

asyncio.run(main())


import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from python.harness import db

async def main():
    await db.init_pool()
    row = await db.fetchrow("SELECT id, config FROM agents WHERE is_active = TRUE")
    if not row:
        print("No active agent found.")
        return

    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)

    current_prompt = cfg.get("system_prompt", "")

    action_directive = (
        "\n\nEXECUTION DIRECTIVE (ACTION OVER PERMISSION):\n"
        "When the user requests a feature, fix, or task, DO NOT spend multiple turns giving status updates or asking 'Want me to proceed?'. "
        "Immediately inspect the required code, write/apply the file edits using your harness tools, and complete the request directly in 1-2 turns. "
        "Never stall in endless documentation reads or reconnaissance loops."
    )

    if "EXECUTION DIRECTIVE" not in current_prompt:
        cfg["system_prompt"] = current_prompt + action_directive
        await db.execute("UPDATE agents SET config = $1 WHERE id = $2", json.dumps(cfg), row["id"])
        print("Successfully updated Tala active agent prompt in database!")
    else:
        print("Execution directive already present in active agent prompt.")

if __name__ == "__main__":
    asyncio.run(main())

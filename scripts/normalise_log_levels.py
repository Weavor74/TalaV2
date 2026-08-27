"""
normalise_log_levels.py — one spelling per log level in agent_log.

``WARN`` and ``WARNING`` were both in use: 98 rows against 16,589. Two
spellings of one level is a filter that looks like it works — a query for
either finds real rows, so nothing looks broken, and it silently misses the
other set.

New writes are normalised in ``db.log_to_db``. This fixes the rows already
there. Dry-run by default, because it edits live data.

    python scripts/normalise_log_levels.py            # report only
    python scripts/normalise_log_levels.py --apply    # rewrite
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def main(apply: bool) -> int:
    import asyncpg

    from python.harness.config import settings
    from python.harness.db import _LOG_LEVEL_ALIASES

    conn = await asyncpg.connect(dsn=settings.DB_URL, timeout=15)
    try:
        rows = await conn.fetch(
            "SELECT level, COUNT(*) n FROM agent_log GROUP BY level ORDER BY n DESC"
        )
        print("agent_log levels:")
        for row in rows:
            print(f"   {row['level']:12} {row['n']:>7}")

        changed = 0
        print()
        for row in rows:
            target = _LOG_LEVEL_ALIASES.get(row["level"])
            if not target or target == row["level"]:
                continue
            print(f"   {row['level']} -> {target}  ({row['n']} row(s))")
            if apply:
                await conn.execute(
                    "UPDATE agent_log SET level = $1 WHERE level = $2",
                    target, row["level"],
                )
            changed += row["n"]

        if not changed:
            print("   nothing to normalise")
    finally:
        await conn.close()

    print()
    print(f"{'normalised' if apply else 'would normalise'}: {changed} row(s)")
    if not apply and changed:
        print("\nDry run. Re-run with --apply to write the changes.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the rows (default is a dry run)")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))

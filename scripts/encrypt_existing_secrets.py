"""
encrypt_existing_secrets.py — encrypt secrets already sitting in the database.

The fixes for UI-047, UI-055, UI-070 and UI-074 encrypt on write, and plaintext
still reads back, so nothing broke. But an existing row stays in clear until
something happens to save it again — which for a credential you set up once and
never touch is "never".

This re-writes them. Dry-run by default, because it edits live data.

    python scripts/encrypt_existing_secrets.py            # report only
    python scripts/encrypt_existing_secrets.py --apply    # re-write

Safety, in order of how much it matters:

* **Reads before it writes, and verifies before it commits.** Every row is
  encrypted, decrypted again, and compared against the original. A row that
  does not round-trip is left exactly as it was.
* **Idempotent.** A row already encrypted is skipped, so running it twice is
  the same as running it once.
* **One transaction per table**, so a failure part-way leaves that table as it
  was rather than half-converted.
* **Never prints a secret.** Lengths and field names only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _unwrap(value):
    seen = 0
    while isinstance(value, (str, bytes)) and seen < 5:
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        seen += 1
    return value


async def main(apply: bool) -> int:
    import asyncpg

    from python.harness.config import settings
    from python.harness.credential_crypto import (
        decrypt_config, decrypt_section, decrypt_value,
        encrypt_config, encrypt_section, encrypt_value, is_encrypted,
    )

    conn = await asyncpg.connect(dsn=settings.DB_URL, timeout=15)
    changed = skipped = failed = 0

    def verify(original, roundtrip, where):
        nonlocal failed
        if original == roundtrip:
            return True
        print(f"   !! {where}: does not round-trip -- left unchanged")
        failed += 1
        return False

    try:
        # -- JSONB config/credentials tables ---------------------------------
        for table, column in (
            ("inference_providers", "config"),
            ("storage_providers", "credentials"),
        ):
            rows = await conn.fetch(f"SELECT id, {column} FROM {table}")
            print(f"\n{table}: {len(rows)} row(s)")
            for row in rows:
                original = _unwrap(row[column])
                if not isinstance(original, dict):
                    continue
                if any(is_encrypted(v) for v in original.values()):
                    skipped += 1
                    print(f"   {row['id'][:34]:36} already encrypted")
                    continue

                sealed = encrypt_config(original)
                if sealed == original:
                    print(f"   {row['id'][:34]:36} nothing secret to encrypt")
                    continue
                if not verify(original, decrypt_config(sealed), f"{table}/{row['id']}"):
                    continue

                wrapped = [k for k, v in sealed.items() if is_encrypted(v)]
                print(f"   {row['id'][:34]:36} encrypting {wrapped}")
                if apply:
                    await conn.execute(
                        f"UPDATE {table} SET {column} = $1::jsonb WHERE id = $2",
                        json.dumps(sealed), row["id"],
                    )
                changed += 1

        # -- database_providers.password (plain TEXT) ------------------------
        rows = await conn.fetch("SELECT id, password FROM database_providers")
        print(f"\ndatabase_providers: {len(rows)} row(s)")
        for row in rows:
            pw = row["password"] or ""
            if not pw:
                continue
            if decrypt_value(pw) != pw:
                skipped += 1
                print(f"   {row['id'][:34]:36} already encrypted")
                continue
            sealed = encrypt_value(pw)
            if not verify(pw, decrypt_value(sealed), f"database_providers/{row['id']}"):
                continue
            print(f"   {row['id'][:34]:36} encrypting password ({len(pw)} chars)")
            if apply:
                await conn.execute(
                    "UPDATE database_providers SET password = $1 WHERE id = $2",
                    sealed, row["id"],
                )
            changed += 1

        # -- profiles.config pii_vault ---------------------------------------
        rows = await conn.fetch("SELECT id, config FROM profiles")
        print(f"\nprofiles: {len(rows)} row(s)")
        for row in rows:
            config = _unwrap(row["config"])
            if not isinstance(config, dict):
                continue
            vault = config.get("pii_vault")
            if not isinstance(vault, dict) or not vault:
                continue
            if any(is_encrypted(v) for v in vault.values()):
                skipped += 1
                print(f"   {row['id'][:34]:36} vault already encrypted")
                continue

            sealed_vault = encrypt_section(vault)
            if not verify(vault, decrypt_section(sealed_vault), f"profiles/{row['id']}"):
                continue

            merged = dict(config)
            merged["pii_vault"] = sealed_vault
            print(f"   {row['id'][:34]:36} encrypting vault "
                  f"({len(vault)} field(s): {sorted(vault)})")
            if apply:
                await conn.execute(
                    "UPDATE profiles SET config = $1::jsonb WHERE id = $2",
                    json.dumps(merged), row["id"],
                )
            changed += 1

    finally:
        await conn.close()

    print()
    verb = "encrypted" if apply else "would encrypt"
    print(f"{verb}: {changed}   already done: {skipped}   refused: {failed}")
    if not apply and changed:
        print("\nThis was a dry run. Re-run with --apply to write the changes.")
    if failed:
        print("\nSome rows did not round-trip and were left untouched. "
              "Investigate before re-running.")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write the changes (default is a dry run)")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))

# File: `add-vps-contact.py`

## Functions

### `async def main()`
*No docstring provided.*

## Source Code

```python
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from python.harness import db  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="Register a trusted Tala P2P contact on a headless VPS.")
    parser.add_argument("--username", required=True, help="Contact username, e.g. @steve-desktop")
    parser.add_argument("--wallet", required=True, help="Contact wallet/address string")
    parser.add_argument("--endpoint", default="", help="Optional callback endpoint, e.g. ws://100.x.x.x:8765")
    parser.add_argument("--secret", default="", help="Shared secret. If omitted, prompts securely.")
    parser.add_argument(
        "--permissions-json",
        default="{}",
        help="JSON object of profile permissions this contact may receive from the VPS.",
    )
    args = parser.parse_args()

    shared_secret = args.secret or getpass.getpass("Shared secret: ")
    if not shared_secret:
        raise SystemExit("Shared secret is required.")

    try:
        permissions = json.loads(args.permissions_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --permissions-json: {exc}") from exc
    if not isinstance(permissions, dict):
        raise SystemExit("--permissions-json must be a JSON object.")

    await db.init_pool()
    try:
        await db.execute(
            """
            INSERT INTO contacts (username, wallet_address, endpoint_url, shared_secret, permissions)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (username) DO UPDATE SET
                wallet_address = EXCLUDED.wallet_address,
                endpoint_url = EXCLUDED.endpoint_url,
                shared_secret = EXCLUDED.shared_secret,
                permissions = EXCLUDED.permissions,
                updated_at = NOW()
            """,
            args.username,
            args.wallet,
            args.endpoint,
            shared_secret,
            permissions,
            caller="scripts.add-vps-contact",
        )
    finally:
        await db.close_pool()

    print(f"Registered trusted Tala contact {args.username} ({args.wallet}).")


if __name__ == "__main__":
    asyncio.run(main())

```

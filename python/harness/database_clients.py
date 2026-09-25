"""
database_clients.py — user databases are a capability, not a policy.

Steven:

    "This is much like git or storage. It does not need to be used. You can
    have 0 databases or 1000 databases. How you use them is up to the user not
    to the develouper."

Which settles UI-071, UI-072 and UI-110 together, and settles them differently
from how the findings framed them.

The panel asserted *"Priority Order — DB connections are queried in the
sequence defined"* and *"Automatic Failover — if the primary replica database
goes offline, operations automatically reroute"*. Neither existed: there is one
pool, from one DSN, created once, so failover had nowhere to live (UI-072). And
UI-110 recorded that Steven wanted content partitioning instead -- coding data
in one database, conversation data in another.

But that was his *example of a use*, not a specification. Building partitioning
into the harness would replace one imposed policy with another. So this module
does what git and storage do: connect, prove the connection is real, and get
out of the way. No priority, no failover, no routing. The user decides what
each connection is for.

Tala's own storage is unaffected and deliberately separate: ``settings.DB_URL``
remains the single pool the harness runs on, and nothing here touches it. A
user database going away must not be able to stop the app.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass
class Driver:
    """One database kind, and whether it can be reached from this install."""

    kind: str
    label: str
    module: str | None = None
    extra: str | None = None
    implemented: bool = True
    notes: str = ""

    @property
    def dependency_present(self) -> bool:
        if not self.module:
            return True
        try:
            importlib.import_module(self.module)
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self.implemented and self.dependency_present

    def describe(self) -> dict[str, Any]:
        if self.available:
            reason = ""
        elif not self.implemented:
            reason = (
                f"{self.label} is not implemented in this build. It needs "
                f"{self.module!r} (pip install \"tala[{self.extra}]\"); the driver "
                f"is deliberately not written against a library that is absent "
                f"and cannot be tested."
            )
        else:
            reason = (
                f"{self.label} needs the {self.module!r} package. "
                f"Install it with:  pip install \"tala[{self.extra}]\""
            )
        return {
            "kind": self.kind, "label": self.label,
            "available": self.available, "implemented": self.implemented,
            "dependency": self.module, "dependency_present": self.dependency_present,
            "install_extra": self.extra, "reason": reason, "notes": self.notes,
        }


DRIVERS: dict[str, Driver] = {
    "postgres": Driver(
        "postgres", "PostgreSQL",
        notes="Uses asyncpg, already a dependency because Tala's own store is "
              "Postgres. A user connection is a separate connection: it does "
              "not share or disturb the harness pool.",
    ),
    "sqlite": Driver(
        "sqlite", "SQLite",
        notes="Uses sqlite3 from the standard library. A file path rather than "
              "a host, and it travels with a portable install.",
    ),
    "mysql": Driver("mysql", "MySQL / MariaDB", module="aiomysql",
                    extra="mysql", implemented=False),
    "mongodb": Driver("mongodb", "MongoDB", module="pymongo",
                      extra="mongodb", implemented=False),
    "mssql": Driver("mssql", "SQL Server", module="aioodbc",
                    extra="mssql", implemented=False),
    "oracle": Driver("oracle", "Oracle", module="oracledb",
                     extra="oracle", implemented=False),
}


def capabilities() -> list[dict[str, Any]]:
    return [d.describe() for d in DRIVERS.values()]


def normalise_kind(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "postgresql": "postgres", "psql": "postgres", "pg": "postgres",
        "mariadb": "mysql", "sql server": "mssql", "sqlserver": "mssql",
        "mongo": "mongodb", "sqlite3": "sqlite",
    }
    return aliases.get(text, text)


def _infer_kind(provider: dict[str, Any]) -> str:
    """
    Work out the kind from whatever the panel captured.

    The panel has no explicit type field, so it is inferred from the endpoint
    or the name. Guessing is stated rather than hidden: an unrecognised
    connection is refused with what it saw, not silently treated as Postgres.
    """
    for source in (provider.get("kind"), provider.get("provider_type")):
        if source:
            kind = normalise_kind(source)
            if kind in DRIVERS:
                return kind

    endpoint = str(provider.get("endpoint") or "")
    scheme = (urlparse(endpoint).scheme or "").lower()
    if scheme.startswith("postgres"):
        return "postgres"
    if scheme.startswith(("mysql", "mariadb")):
        return "mysql"
    if scheme.startswith("mongodb"):
        return "mongodb"
    if scheme == "sqlite" or endpoint.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite"

    haystack = f"{endpoint} {provider.get('name', '')}".lower()
    for kind in DRIVERS:
        if kind in haystack:
            return kind
    return ""


class DatabaseError(RuntimeError):
    """A database operation failed, with a reason fit to show a person."""


def _dsn_for(provider: dict[str, Any], kind: str) -> str:
    endpoint = str(provider.get("endpoint") or "").strip()
    if not endpoint:
        raise DatabaseError("This connection has no endpoint configured.")
    if "://" in endpoint:
        return endpoint

    user = str(provider.get("username") or "")
    password = str(provider.get("password") or "")
    database = str(provider.get("database") or "")
    host, _, port = endpoint.partition(":")
    scheme = "postgresql" if kind == "postgres" else kind
    auth = f"{user}:{password}@" if user else ""
    return f"{scheme}://{auth}{host}:{port or 5432}/{database}"


async def probe(provider: dict[str, Any]) -> str:
    """
    Open a real connection and prove it works, or say exactly why not.

    A saved connection is not a working one. This is the whole difference
    between the panel's old hardcoded ONLINE badge and a measurement.
    """
    kind = _infer_kind(provider)
    if not kind:
        raise DatabaseError(
            f"Could not tell what kind of database "
            f"{provider.get('endpoint') or provider.get('name') or 'this'} is. "
            f"Use a URL with a scheme, such as postgresql://host/db."
        )
    driver = DRIVERS[kind]
    if not driver.available:
        raise DatabaseError(driver.describe()["reason"])

    if kind == "postgres":
        import asyncpg
        try:
            conn = await asyncpg.connect(dsn=_dsn_for(provider, kind), timeout=10)
        except Exception as exc:
            raise DatabaseError(f"{type(exc).__name__}: {exc}") from exc
        try:
            version = await conn.fetchval("SELECT version()")
            return f"Connected. {str(version or '')[:80]}"
        finally:
            await conn.close()

    if kind == "sqlite":
        import sqlite3
        path = str(provider.get("endpoint") or "").replace("sqlite://", "")
        try:
            conn = sqlite3.connect(path, timeout=10)
        except Exception as exc:
            raise DatabaseError(f"{type(exc).__name__}: {exc}") from exc
        try:
            version = conn.execute("SELECT sqlite_version()").fetchone()
            return f"Connected. SQLite {version[0] if version else '?'} at {path}"
        finally:
            conn.close()

    raise DatabaseError(f"{driver.label} has no driver in this build.")

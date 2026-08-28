"""
storage_clients.py — storage providers become an option that actually exists.

Steven:

    "A user may never install or consume any of the storage/git/database
    providers but they should have the option too"

Which reframes UI-068 correctly. The problem was never that nine provider types
were offered; it was that **none of them could work**, and nothing said so. The
panel accepted a bucket name and a secret key and stored them, and no code
anywhere could talk to a bucket.

So the answer is not "ship nine cloud SDKs on a USB stick" and not "offer
fewer". It is: make the capability real where it costs nothing, declare the
rest as optional extras, and be exact about which is which. A user who never
touches storage carries no extra weight; a user who wants S3 is told the one
command that gets it.

Three types work with no new dependency at all:

* **folder** — a local or network path. For a portable install this is the
  useful one: backing the stick up to a NAS or a second drive.
* **ftp** / **ftps** — ``ftplib`` is in the standard library.
* **webdav** — ``httpx`` is already a dependency.

The rest declare what they need. They are *not* implemented against an
uninstalled library and hopefully-correct code: an adapter written blind is
exactly the kind of thing this audit keeps finding. They report what to
install, and refuse until it is there.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Capability:
    """What one provider type needs, and whether it is usable right now."""

    kind: str
    label: str
    module: str | None = None          # the import that must succeed
    extra: str | None = None           # pip extra that provides it
    implemented: bool = True           # is there code behind it in this build
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
            # Name the dependency anyway. "Not implemented" on its own tells
            # the reader nothing they can act on, and the extra is declared in
            # pyproject whether or not the adapter exists yet.
            reason = (
                f"{self.label} is not implemented in this build. It needs "
                f"{self.module!r} (pip install \"tala[{self.extra}]\"), and the "
                f"adapter is deliberately not written against a library that is "
                f"absent and cannot be tested -- an adapter written blind is the "
                f"kind of thing this codebase keeps finding."
            )
        else:
            reason = (
                f"{self.label} needs the {self.module!r} package. "
                f"Install it with:  pip install \"tala[{self.extra}]\""
            )
        return {
            "kind": self.kind,
            "label": self.label,
            "available": self.available,
            "implemented": self.implemented,
            "dependency": self.module,
            "dependency_present": self.dependency_present,
            "install_extra": self.extra,
            "reason": reason,
            "notes": self.notes,
        }


CAPABILITIES: dict[str, Capability] = {
    "folder": Capability(
        "folder", "Local or network folder",
        notes="No dependency. For a portable install this is the useful one: "
              "backing the stick up to a NAS or a second drive.",
    ),
    "ftp": Capability(
        "ftp", "FTP / FTPS",
        notes="Uses ftplib from the standard library. FTPS is used when the "
              "provider sets tls.",
    ),
    "webdav": Capability(
        "webdav", "WebDAV",
        notes="Uses httpx, already a dependency. Nextcloud, ownCloud and most "
              "NAS boxes speak this.",
    ),
    "s3": Capability(
        "s3", "S3-compatible (AWS, MinIO, Backblaze, Spaces)",
        module="boto3", extra="s3", implemented=False,
    ),
    "sftp": Capability(
        "sftp", "SFTP over SSH",
        module="paramiko", extra="sftp", implemented=False,
    ),
    "azure": Capability(
        "azure", "Azure Blob Storage",
        module="azure.storage.blob", extra="azure", implemented=False,
    ),
    "dropbox": Capability(
        "dropbox", "Dropbox", module="dropbox", extra="dropbox", implemented=False,
    ),
    "gdrive": Capability(
        "gdrive", "Google Drive",
        module="googleapiclient", extra="gdrive", implemented=False,
    ),
    "onedrive": Capability(
        "onedrive", "OneDrive", module="msal", extra="onedrive", implemented=False,
    ),
}


def capabilities() -> list[dict[str, Any]]:
    """Every provider type, and whether it can be used right now."""
    return [cap.describe() for cap in CAPABILITIES.values()]


def normalise_kind(provider_type: str) -> str:
    """Map a panel's provider_type string onto a capability key."""
    text = str(provider_type or "").strip().lower()
    aliases = {
        "s3-compatible": "s3", "s3 compatible": "s3", "aws": "s3", "minio": "s3",
        "backblaze": "s3", "spaces": "s3", "idrive": "s3",
        "sftp / ssh": "sftp", "ssh": "sftp",
        "ftp/ftps": "ftp", "ftps": "ftp",
        "google drive": "gdrive", "googledrive": "gdrive",
        "azure blob": "azure", "azure blob storage": "azure",
        "local": "folder", "nfs": "folder", "network folder": "folder",
        "custom": "webdav",
    }
    return aliases.get(text, text)


class StorageError(RuntimeError):
    """A storage operation failed, with a reason fit to show a person."""


def get_client(provider: dict[str, Any]):
    """
    A client for one configured provider, or a refusal that says why.

    Never returns a stub that pretends to work. The whole finding this closes
    was a panel that accepted credentials for nine backends none of which
    existed.
    """
    kind = normalise_kind(provider.get("provider_type"))
    cap = CAPABILITIES.get(kind)
    if cap is None:
        raise StorageError(
            f"Unknown storage type {provider.get('provider_type')!r}. "
            f"Known types: {', '.join(sorted(CAPABILITIES))}."
        )
    if not cap.available:
        raise StorageError(cap.describe()["reason"])

    creds = provider.get("credentials") or {}
    if kind == "folder":
        return FolderClient(creds)
    if kind == "ftp":
        return FtpClient(creds)
    if kind == "webdav":
        return WebdavClient(creds)
    raise StorageError(f"{cap.label} has no client in this build.")


# ---------------------------------------------------------------------------
# Clients. Each implements probe() and put(), which is all the backup path
# needs; read-back is a separate job and is not pretended at here.
# ---------------------------------------------------------------------------

class FolderClient:
    """A local or network directory."""

    def __init__(self, creds: dict[str, Any]):
        raw = str(creds.get("path") or creds.get("bucket") or "").strip()
        if not raw:
            raise StorageError("This folder provider has no 'path' configured.")
        self.root = Path(raw).expanduser()

    def probe(self) -> str:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".tala-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise StorageError(f"{self.root} is not writable: {exc}") from exc
        return f"Writable: {self.root}"

    def put(self, name: str, data: bytes) -> str:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so an interrupted backup cannot leave a truncated
        # file that looks like a complete one.
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return str(target)


class FtpClient:
    """FTP or FTPS via the standard library."""

    def __init__(self, creds: dict[str, Any]):
        self.host = str(creds.get("host") or creds.get("endpoint") or "").strip()
        if not self.host:
            raise StorageError("This FTP provider has no 'host' configured.")
        self.port = int(creds.get("port") or 21)
        self.user = str(creds.get("username") or "anonymous")
        self.password = str(creds.get("password") or "")
        self.root = str(creds.get("path") or "").strip("/")
        self.tls = bool(creds.get("tls"))

    def _connect(self):
        import ftplib
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        client = cls()
        client.connect(self.host, self.port, timeout=15)
        client.login(self.user, self.password)
        if self.tls:
            client.prot_p()
        if self.root:
            client.cwd(self.root)
        return client

    def probe(self) -> str:
        import ftplib
        try:
            client = self._connect()
        except Exception as exc:
            raise StorageError(f"{type(exc).__name__}: {exc}") from exc
        try:
            welcome = client.getwelcome() or ""
            return f"Connected to {self.host}:{self.port}. {welcome[:80]}"
        finally:
            try:
                client.quit()
            except Exception:
                client.close()

    def put(self, name: str, data: bytes) -> str:
        import io
        client = self._connect()
        try:
            client.storbinary(f"STOR {name}", io.BytesIO(data))
            return f"ftp://{self.host}/{self.root}/{name}".replace("//", "/")
        finally:
            try:
                client.quit()
            except Exception:
                client.close()


class WebdavClient:
    """WebDAV over httpx, which is already a dependency."""

    def __init__(self, creds: dict[str, Any]):
        self.base = str(creds.get("endpoint") or creds.get("host") or "").strip().rstrip("/")
        if not self.base:
            raise StorageError("This WebDAV provider has no 'endpoint' configured.")
        self.user = str(creds.get("username") or "")
        self.password = str(creds.get("password") or "")

    def _auth(self):
        return (self.user, self.password) if self.user else None

    def probe(self) -> str:
        import httpx
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                response = client.request(
                    "PROPFIND", self.base + "/", auth=self._auth(),
                    headers={"Depth": "0"},
                )
        except Exception as exc:
            raise StorageError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (200, 207):
            return f"WebDAV responded {response.status_code} at {self.base}"
        if response.status_code in (401, 403):
            raise StorageError(
                f"{self.base} rejected the credentials (HTTP {response.status_code})."
            )
        raise StorageError(f"{self.base} answered HTTP {response.status_code}.")

    def put(self, name: str, data: bytes) -> str:
        import httpx
        url = f"{self.base}/{name.lstrip('/')}"
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.put(url, content=data, auth=self._auth())
        if response.status_code >= 400:
            raise StorageError(f"PUT {url} answered HTTP {response.status_code}.")
        return url

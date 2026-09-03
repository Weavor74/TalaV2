"""
git_ops.py — the git panel does clone, pull and push, for the user's repos.

The panel said: *"Higher priority providers are used first for clone, push, and
pull operations."* `git_providers` was referenced in six places, every one of
them storage or display, so none of those three operations existed (UI-067).

Steven, on whose git this is:

    "the GIT panel should stay. But Tala does not use GIT for herself."

So this operates on the **user's** repositories. It is not, and must not
become, a way for the harness to version itself -- that path is
``safe_code_bridges`` and its snapshot mechanism, and conflating the two is how
an auto-rollback once deleted 427 tracked files.

**How the credential is passed matters more than anything else here.** Three
obvious approaches each leak it:

* putting the token in the remote URL writes it, in clear, into
  ``.git/config`` -- permanently, and into any backup of the repo;
* ``-c http.extraHeader=...`` puts it in ``argv``, where any process listing
  shows it;
* a credential helper script has to be written to disk.

Git 2.31+ reads config from ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` /
``GIT_CONFIG_VALUE_n``. Environment variables are not in the command line and
are not written to the repository, so that is what is used. The token exists in
the child process's environment for the length of one command and nowhere else.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Operations this module will run. Anything not listed is refused rather than
#: passed through: a general "run git with these arguments" call is a shell.
ALLOWED_COMMANDS = ("status", "clone", "pull", "push", "fetch", "log", "remote")

DEFAULT_TIMEOUT = 120


class GitError(RuntimeError):
    """A git operation failed, with output fit to show a person."""


def git_available() -> tuple[bool, str]:
    path = shutil.which("git")
    return (bool(path), path or "git is not on PATH")


def workspace_root() -> Path:
    """
    Where clones are allowed to live.

    Under the portable data root, so a repository cloned on one machine travels
    with the install like everything else.
    """
    from sdk.agent.documentation_nodes import resolve_user_data_dir
    return resolve_user_data_dir() / "repos"


def resolve_path(relative: str) -> Path:
    """
    Resolve a caller-supplied repository path inside the workspace.

    Containment is checked after resolution so a symlink cannot be used to
    climb out, which is the same rule the file sandbox uses.
    """
    raw = str(relative or "").strip()
    # Treat a backslash as a separator too. On POSIX "\" is a legal filename
    # character, so "..\\..\\etc" would otherwise be swallowed into a single
    # name and sail past the containment check below -- the same traversal that
    # is caught on Windows must be caught here, on whatever OS the portable
    # install happens to be running.
    normalized = raw.replace("\\", "/")
    # Refuse an absolute path rather than quietly making it relative. Stripping
    # the leading separator would turn "/etc/passwd" into "<workspace>/etc/passwd"
    # -- contained, but not what the caller asked for, and a rule that silently
    # reinterprets its input is one nobody can reason about. Absolute covers
    # POSIX ("/etc"), a Windows drive ("C:\\Windows") and a UNC share
    # ("\\\\server\\share", now a leading "//") on every platform.
    if (
        Path(normalized).is_absolute()
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise GitError(
            f"{relative!r} is an absolute path. Repository names are relative to "
            f"the workspace."
        )
    root = workspace_root().resolve()
    candidate = (root / normalized).resolve()
    if candidate != root and root not in candidate.parents:
        raise GitError(
            f"{relative!r} resolves outside the repository workspace ({root})."
        )
    return candidate


def _auth_env(provider: dict[str, Any] | None) -> dict[str, str]:
    """
    Environment carrying the credential, if there is one.

    Returns an empty dict for an anonymous clone rather than inventing a header
    -- a public repository must not require a provider to be configured.
    """
    if not provider:
        return {}
    username = str(provider.get("username") or "").strip()
    token = str(provider.get("api_key") or "").strip()
    if not token:
        return {}

    # Git accepts a token as the password with any non-empty username on every
    # major host; the username is only meaningful to some of them.
    basic = base64.b64encode(
        f"{username or 'x-access-token'}:{token}".encode("utf-8")
    ).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        # Never let git stop for a prompt: a hung process is worse than a
        # clean failure, and there is no terminal to answer on.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
    }


async def run_git(
    args: list[str],
    cwd: Path | None = None,
    provider: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one git command and return its result. Never raises on non-zero."""
    if not args or args[0] not in ALLOWED_COMMANDS:
        raise GitError(
            f"Refusing to run 'git {args[0] if args else ''}'. Permitted: "
            f"{', '.join(ALLOWED_COMMANDS)}."
        )
    available, where = git_available()
    if not available:
        raise GitError(f"git is not available: {where}")

    # A remote URL among the arguments is an outbound connection, so it goes
    # through the same guard as everything else (UI-063). Local operations --
    # status, log -- carry no URL and are unaffected.
    for argument in args[1:]:
        if "://" in str(argument):
            from python.harness.network_guard import NetworkBlocked, assert_may_reach
            from python.harness.access import AGENT, Principal
            try:
                await assert_may_reach(
                    str(argument), Principal(AGENT, "default"),
                    purpose=f"git {args[0]}")
            except NetworkBlocked as exc:
                raise GitError(str(exc)) from exc

    import os
    env = dict(os.environ)
    env.update(_auth_env(provider))
    env.setdefault("GIT_TERMINAL_PROMPT", "0")

    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise GitError(
            f"git {args[0]} did not finish within {timeout}s. It may be waiting "
            f"for credentials that were not supplied."
        ) from None
    except OSError as exc:
        raise GitError(f"Could not run git: {exc}") from exc

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    return {
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "stdout": out,
        # Scrubbed, because a failure message can echo the remote URL and some
        # hosts include a token in redirects.
        "stderr": _scrub(err),
        "command": f"git {' '.join(args)}",
    }


def _scrub(text: str) -> str:
    """Remove anything that looks like a credential from git's own output."""
    import re
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", text)
    return re.sub(r"(?i)(authorization:\s*basic\s+)\S+", r"\1***", text)


async def providers_for(url: str) -> list[dict[str, Any]]:
    """
    Configured providers that could serve this URL, highest priority first.

    Matched on host so a GitHub token is not offered to a GitLab remote. A URL
    with no matching provider still returns an empty list rather than an error:
    public repositories need no credential.
    """
    from python.harness import db
    from python.harness.credential_crypto import decrypt_config

    host = (urlparse(str(url or "")).hostname or "").lower()
    try:
        rows = await db.fetch(
            "SELECT p.id, p.name, p.endpoint, p.username, p.api_key, p.priority, "
            "p.credential_id, c.config AS cred_config "
            "FROM git_providers p "
            "LEFT JOIN auth_credentials c ON p.credential_id = c.id "
            "ORDER BY p.priority ASC"
        )
    except Exception as exc:
        log.warning("git: could not read providers: %s", exc)
        return []

    matches = []
    for row in rows:
        endpoint_host = (urlparse(row["endpoint"] or "").hostname or "").lower()
        if host and endpoint_host and host != endpoint_host:
            continue
        api_key = row["api_key"]
        if row["credential_id"] and row["cred_config"]:
            cfg = decrypt_config(row["cred_config"]) or {}
            api_key = cfg.get("api_key") or cfg.get("token") or api_key
        matches.append({
            "id": row["id"], "name": row["name"], "endpoint": row["endpoint"],
            "username": row["username"], "api_key": api_key,
            "priority": row["priority"],
        })
    return matches


async def clone(url: str, directory: str = "", provider: dict | None = None) -> dict[str, Any]:
    """Clone into the repository workspace."""
    url = str(url or "").strip()
    if not url:
        raise GitError("A repository URL is required.")

    name = directory.strip() if directory else Path(urlparse(url).path).stem
    if not name:
        raise GitError("Could not work out a directory name from that URL.")

    target = resolve_path(name)
    if target.exists():
        raise GitError(f"{target.name} already exists in the workspace.")
    target.parent.mkdir(parents=True, exist_ok=True)

    if provider is None:
        candidates = await providers_for(url)
        provider = candidates[0] if candidates else None

    result = await run_git(["clone", url, str(target)], provider=provider)
    result["path"] = str(target)
    result["provider"] = provider["name"] if provider else None
    if not result["success"]:
        # Do not leave a half-clone that looks like a repository.
        if target.exists() and not (target / ".git").exists():
            shutil.rmtree(target, ignore_errors=True)
    return result

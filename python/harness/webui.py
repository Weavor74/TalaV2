"""
python/harness/webui.py — the Electron renderer, served to a browser.

WHY THIS IS SMALL
-----------------
The renderer was already a web app. It is plain HTML/CSS/JS with no Node and
no Electron API beyond a single object, ``window.tala``, which preload.js
exposes over IPC. The harness has always been a WebSocket JSON-RPC server.

So the gap between "desktop app" and "web app" was never the UI. It was that
one object, plus the handful of filesystem operations Electron's main process
performed on the renderer's behalf. ``renderer/tala-web-shim.js`` supplies the
object; this module serves the files and performs those operations.

The renderer itself is not modified. The same index.html is served to Electron
and to the browser, and the shim is injected at request time.

WHAT IS DELIBERATELY NOT MIRRORED
---------------------------------
``terminal:kill`` has no equivalent: the harness owns the shell rather than
handing out per-session process handles, and inventing one here would be
guessing at semantics. The shim rejects that call by name. Everything else in
the preload whitelist has a real implementation.

SECURITY
--------
Serving this UI exposes 247 JSON-RPC handlers, several of which run shell
commands, patch source files and read the vault. The defaults are therefore:

  * bind 127.0.0.1 -- the loopback interface only
  * binding anywhere else requires TALA_WEB_TOKEN to be set, and the server
    refuses to start without it rather than starting insecurely

That refusal is the point. A convenience server that quietly listens on every
interface is how a local tool becomes a remote shell.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

#: Mirrors ipc-handlers.js so the two cannot drift apart silently.
MAX_READ_BYTES = 10 * 1024 * 1024
MAX_LIST_ENTRIES = 5000
LIST_IGNORED_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "dist",
    ".pytest_cache", ".tala_snapshots",
}

_RENDERER_DIR = Path(__file__).resolve().parents[2] / "renderer"

# Both are external files, not an inline <script>. index.html sets a strict
# Content-Security-Policy of script-src 'self', so an injected inline block is
# refused by the browser and never runs. That failed almost invisibly: the shim
# fell back to its default WebSocket URL and connected anyway, so the UI came
# up working while the config -- including the auth token -- had silently never
# been delivered.
_SHIM_TAG = (
    '<script src="/tala-web-config.js"></script>\n'
    '  <script src="/tala-web-shim.js"></script>\n'
)


def _data_root() -> Path:
    """The confinement root, matching Electron's ``userData``."""
    try:
        from sdk.agent.documentation_nodes import resolve_user_data_dir
        return Path(resolve_user_data_dir())
    except Exception:
        return Path(__file__).resolve().parents[2]


def _safe_path(root: Path, candidate: str) -> Path:
    """
    Resolve *candidate* under *root*, refusing anything that escapes it.

    The Electron side calls this safePath(). Same rule: a relative path is
    joined to the data root, and the result must still be inside it after
    symlinks are resolved. ``..`` traversal and absolute paths outside the
    root are rejected rather than clamped, because silently rewriting a path
    the caller asked for is its own kind of surprise.
    """
    if candidate is None:
        raise ValueError("a file path is required")
    target = (root / str(candidate)).resolve() if not Path(candidate).is_absolute() \
        else Path(candidate).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise PermissionError(
            f"path {candidate!r} resolves outside the data root and was refused"
        )
    return target


def _file_url(path: Path) -> str:
    formatted = str(path).replace("\\", "/")
    return "file:///" + (formatted[1:] if formatted.startswith("/") else formatted)


# ---------------------------------------------------------------------------
# Bridge channels — what Electron's main process used to do
# ---------------------------------------------------------------------------

def _bridge_file_read(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(root, payload.get("filePath"))
    if not target.is_file():
        raise ValueError(f"{payload.get('filePath')!r} is not a regular file.")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"File exceeds maximum read size ({MAX_READ_BYTES} bytes).")
    data = target.read_text(encoding=payload.get("encoding") or "utf-8", errors="replace")
    return {"ok": True, "data": data, "size": size,
            "absolutePath": str(target), "fileUrl": _file_url(target)}


def _bridge_file_write(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if data is None:
        raise ValueError('file:write requires a "data" field.')
    target = _safe_path(root, payload.get("filePath"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(data), encoding=payload.get("encoding") or "utf-8")
    return {"ok": True, "path": str(target), "fileUrl": _file_url(target)}


def _bridge_file_delete(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(root, payload.get("filePath"))
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    return {"ok": True, "path": str(target)}


def _bridge_file_list(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    base = _safe_path(root, payload.get("dir") or ".")
    if not base.exists():
        return {"ok": True, "entries": []}
    if not base.is_dir():
        raise ValueError(f"{payload.get('dir')!r} is not a directory.")

    recursive = bool(payload.get("recursive"))
    with_sizes = bool(payload.get("withSizes"))
    entries: list[dict[str, Any]] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > 20 or len(entries) >= MAX_LIST_ENTRIES:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in children:
            if len(entries) >= MAX_LIST_ENTRIES:
                return
            is_dir = child.is_dir()
            if is_dir and child.name in LIST_IGNORED_DIRS:
                continue
            entry = {
                "name": child.name,
                "relativePath": str(child.relative_to(base)).replace("\\", "/"),
                "type": "directory" if is_dir else ("file" if child.is_file() else "other"),
                "absolutePath": str(child),
                "fileUrl": _file_url(child),
            }
            if with_sizes and child.is_file():
                try:
                    entry["size"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(entry)
            if recursive and is_dir:
                walk(child, depth + 1)

    walk(base, 0)
    return {"ok": True, "entries": entries}


def _bridge_git_status(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-b"],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
        return {"ok": True, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _bridge_vault_read(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Vault reads go through the harness's own vault RPC, not the filesystem.

    Electron read the file directly after checking no outbound connections were
    open. That check does not transfer -- a web server is itself a connection --
    so rather than reimplement a weaker version of it, this refuses and points
    at the RPC the renderer can call, which applies the real vault rules.
    """
    raise PermissionError(
        "vault:read is not served over HTTP. Use the vault.read JSON-RPC "
        "method, which applies the vault's own access rules."
    )


_BRIDGE: dict[str, Any] = {
    "file:read": _bridge_file_read,
    "file:write": _bridge_file_write,
    "file:delete": _bridge_file_delete,
    "file:list": _bridge_file_list,
    "git:status": _bridge_git_status,
    "vault:read": _bridge_vault_read,
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _make_app(ws_port: int, token: str) -> web.Application:
    app = web.Application()
    root = _data_root()

    def authorised(request: web.Request) -> bool:
        if not token:
            return True
        supplied = request.headers.get("X-Tala-Token") or request.query.get("token")
        return secrets.compare_digest(str(supplied or ""), token)

    async def index(request: web.Request) -> web.Response:
        if not authorised(request):
            return web.Response(status=401, text="A token is required.")
        html = (_RENDERER_DIR / "index.html").read_text(encoding="utf-8", errors="replace")
        # Inject before the first script so window.tala exists by the time the
        # renderer's own code runs.
        idx = html.find("<script")
        html = html[:idx] + _SHIM_TAG + "  " + html[idx:] if idx != -1 else html + _SHIM_TAG
        return web.Response(text=html, content_type="text/html")

    async def config(request: web.Request) -> web.Response:
        if not authorised(request):
            return web.Response(status=401, text="// unauthorised")
        import json as _json
        # Two different secrets, deliberately not merged. `token` gates this
        # HTTP surface and is optional on loopback; `wsToken` is what 8765
        # demands of every connection since SEC-W0-03. Reusing one for both
        # would make turning off the convenience gate also turn off the
        # security one.
        from python.harness import ws_auth

        payload = _json.dumps({
            "wsUrl": f"ws://{request.host.split(':')[0]}:{ws_port}",
            "bridgeUrl": "/api/bridge",
            "token": token,
            "wsToken": ws_auth.load_or_create_token(),
        })
        return web.Response(
            text=f"window.__TALA_WEB_CONFIG__ = {payload};",
            content_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    async def shim(request: web.Request) -> web.Response:
        return web.Response(
            text=(_RENDERER_DIR / "tala-web-shim.js").read_text(encoding="utf-8"),
            content_type="application/javascript",
        )

    async def bridge(request: web.Request) -> web.Response:
        if not authorised(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        channel = body.get("channel")
        handler = _BRIDGE.get(channel)
        if handler is None:
            # Naming the channel matters: a bridge that 404s anonymously is
            # indistinguishable from one that is not running.
            return web.json_response(
                {"error": f"channel {channel!r} is not served by the web bridge"},
                status=404,
            )
        try:
            return web.json_response(handler(root, body.get("payload") or {}))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.warning("webui: %s failed: %s", channel, exc)
            return web.json_response({"error": str(exc)}, status=400)

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/tala-web-config.js", config)
    app.router.add_get("/tala-web-shim.js", shim)
    app.router.add_post("/api/bridge", bridge)
    app.router.add_static("/", _RENDERER_DIR, show_index=False)
    return app


async def start(host: str | None = None,
                port: int | None = None,
                ws_port: int | None = None) -> web.AppRunner | None:
    """
    Serve the renderer over HTTP. Returns the runner, or None if disabled.

    Enabled with ``TALA_WEB_UI=1``. Off by default: this is an additional
    listening socket on a machine that may not want one.
    """
    if (os.environ.get("TALA_WEB_UI") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return None

    host = host or os.environ.get("TALA_WEB_HOST") or "127.0.0.1"
    # 8767, not 8766: the harness already binds 8765 (WS_PORT), 8766 (MCP_PORT)
    # and 8000 (VLLM_PORT). Colliding with MCP_PORT produced a web UI that
    # 404'd every request, because the MCP SSE server had the socket and was
    # answering -- a failure that looks exactly like a routing bug.
    port = int(port or os.environ.get("TALA_WEB_PORT") or 8767)
    if ws_port is None:
        try:
            from python.harness.config import settings
            ws_port = int(settings.WS_PORT)
        except Exception:
            ws_port = 8765

    token = (os.environ.get("TALA_WEB_TOKEN") or "").strip()

    # Refuse to serve a remote shell by accident.
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in ("localhost",)
    if not loopback and not token:
        logger.error(
            "Web UI refused to start: host %r is not loopback and TALA_WEB_TOKEN "
            "is unset. This UI reaches 247 RPC handlers including shell "
            "execution and source patching; binding it to a reachable interface "
            "without a token would publish a remote shell. Set TALA_WEB_TOKEN, "
            "or bind 127.0.0.1.", host,
        )
        return None

    return await _serve(host, port, ws_port, token)


async def _serve(host: str, port: int, ws_port: int, token: str) -> web.AppRunner | None:
    runner = web.AppRunner(_make_app(ws_port, token))
    await runner.setup()
    # Deliberately NOT reuse_address. On Windows SO_REUSEADDR lets a second
    # socket bind a port another process is already listening on, with
    # undefined delivery between them -- so a collision stops raising and
    # becomes a server that appears to start and then 404s every request,
    # because the other process is answering. Losing the race must be loud.
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        logger.error(
            "Web UI could not bind %s:%d -- %s. The harness already uses "
            "8765 (WS_PORT), 8766 (MCP_PORT) and 8000 (VLLM_PORT); set "
            "TALA_WEB_PORT to something else.", host, port, exc,
        )
        return None
    logger.info(
        "Web UI listening on http://%s:%d%s (renderer served from %s)",
        host, port, " [token required]" if token else "", _RENDERER_DIR,
    )
    return runner


# ---------------------------------------------------------------------------
# Standalone: serve the UI against a harness that is already running
# ---------------------------------------------------------------------------

async def _main() -> None:
    """
    Serve only the UI, pointing the shim at an existing harness.

    This is the mode to use when Tala is already running -- the desktop app
    spawns its own harness, and starting a second one would collide on the
    WebSocket port and take both down. The renderer does not care which
    process serves its files; the shim connects to the harness over the
    WebSocket either way.
    """
    import asyncio

    host = os.environ.get("TALA_WEB_HOST") or "127.0.0.1"
    port = int(os.environ.get("TALA_WEB_PORT") or 8767)
    try:
        from python.harness.config import settings
        ws_port = int(settings.WS_PORT)
    except Exception:
        ws_port = int(os.environ.get("TALA_WS_PORT") or 8765)
    token = (os.environ.get("TALA_WEB_TOKEN") or "").strip()

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in ("localhost",)
    if not loopback and not token:
        raise SystemExit(
            f"Refusing to serve on {host}: this UI reaches every RPC handler "
            "the harness has, including shell execution. Set TALA_WEB_TOKEN, "
            "or bind 127.0.0.1."
        )

    runner = await _serve(host, port, ws_port, token)
    if runner is None:
        raise SystemExit(1)

    print(f"[tala] web UI on http://{host}:{port} -> harness ws://{host}:{ws_port}")
    print("[tala] press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

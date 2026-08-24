"""
python/harness/channels.py — Outside messaging channels
=======================================================

Lets Tala be reached from outside the desktop app. Each row in
``chat_channels`` (platform, name, enabled, config) becomes a running
adapter; ChannelManager owns their lifecycle and is hot-reloaded whenever
the settings UI saves or deletes one.

Adapters are deliberately poll-based rather than webhook-based: Tala runs
on a laptop behind NAT with no public URL, so long-polling is the only
transport that works everywhere without tunnels or port-forwarding.

An adapter must never take the harness down. Every poll loop catches its
own exceptions, backs off, and keeps going — a revoked Telegram token
should show as an unhealthy channel in settings, not a dead server.

NOTE: Reconstructed 2026-08-12 after an automated rollback deleted it (it
had never been committed). The contract comes from its call sites in
server.py: the module exports ``_ADAPTERS`` (platform -> adapter) and a
``ChannelManager`` singleton with ``get()``, ``reload()``, ``start()``,
``status()`` and ``register_broadcast()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class BaseAdapter:
    """One configured channel. Subclasses implement poll() and send()."""

    platform = "base"

    def __init__(self, channel_id: str, name: str, config: dict[str, Any]) -> None:
        self.channel_id = channel_id
        self.name = name
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_error: str | None = None
        self.messages_seen = 0
        self.messages_rejected = 0

    # -- lifecycle ---------------------------------------------------------

    # -- authorisation -----------------------------------------------------

    def allowlist(self) -> set[str]:
        """Usernames and chat ids permitted to command the agent."""
        raw = self.config.get("allowlist") or self.config.get("allow_list") or ""
        if isinstance(raw, (list, tuple, set)):
            parts = [str(x) for x in raw]
        else:
            parts = str(raw).replace(";", ",").split(",")
        return {p.strip().lstrip("@").lower() for p in parts if str(p).strip()}

    def is_allowed(self, msg: dict[str, Any]) -> bool:
        """
        Fail closed. An empty allowlist permits nobody.

        The field was collected, stored, and never read (UI-083) — so an
        allowlist of any length let the whole world command the agent, in front
        of tool execution. Empty now denies rather than permits, matching the
        invite-only model: access is granted explicitly, never by default.
        """
        allowed = self.allowlist()
        if not allowed:
            return False
        sender = str(msg.get("sender") or "").strip().lstrip("@").lower()
        chat_id = str(msg.get("chat_id") or "").strip().lower()
        return bool((sender and sender in allowed) or (chat_id and chat_id in allowed))

    async def start(self, on_message: Callable[[dict[str, Any]], Any]) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(on_message))
        log.info("channels: started %s adapter %r", self.platform, self.name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self, on_message: Callable[[dict[str, Any]], Any]) -> None:
        backoff = 2.0
        while self._running:
            try:
                for msg in await self.poll():
                    self.messages_seen += 1
                    # Enforced here rather than per adapter: every platform's
                    # messages funnel through this loop, so a future Discord
                    # adapter inherits the check instead of having to repeat it.
                    if not self.is_allowed(msg):
                        self.messages_rejected += 1
                        log.warning(
                            "channels: %s/%s rejected a message from sender=%r chat_id=%r "
                            "(not on the allowlist)",
                            self.platform, self.name,
                            msg.get("sender"), msg.get("chat_id"),
                        )
                        continue
                    result = on_message(msg)
                    if asyncio.iscoroutine(result):
                        await result
                self.last_error = None
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A broken channel is a status line, never a crashed harness.
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("channels: %s adapter %r poll failed: %s", self.platform, self.name, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            await asyncio.sleep(float(self.config.get("poll_seconds", 3.0)))

    # -- transport ---------------------------------------------------------

    async def poll(self) -> list[dict[str, Any]]:
        return []

    async def send(self, text: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "id": self.channel_id,
            "platform": self.platform,
            "name": self.name,
            "running": bool(self._running and self._task and not self._task.done()),
            "messages_seen": self.messages_seen,
            "last_error": self.last_error,
        }


class TelegramAdapter(BaseAdapter):
    """Long-polls getUpdates; no public URL or webhook required."""

    platform = "telegram"

    def __init__(self, channel_id: str, name: str, config: dict[str, Any]) -> None:
        super().__init__(channel_id, name, config)
        self._offset: int | None = None

    def _token(self) -> str:
        return str(self.config.get("bot_token") or self.config.get("token") or "").strip()

    async def poll(self) -> list[dict[str, Any]]:
        import httpx

        token = self._token()
        if not token:
            raise RuntimeError("telegram channel has no bot_token configured")

        params: dict[str, Any] = {"timeout": 0}
        if self._offset is not None:
            params["offset"] = self._offset

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates", params=params
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
            payload = resp.json()

        messages: list[dict[str, Any]] = []
        for update in payload.get("result", []) or []:
            self._offset = int(update.get("update_id", 0)) + 1
            msg = update.get("message") or update.get("channel_post") or {}
            text = msg.get("text")
            if not text:
                continue
            messages.append(
                {
                    "channel_id": self.channel_id,
                    "platform": self.platform,
                    "chat_id": (msg.get("chat") or {}).get("id"),
                    "sender": (msg.get("from") or {}).get("username"),
                    "text": text,
                }
            )
        return messages

    async def send(self, text: str, chat_id: Any = None, **kwargs: Any) -> dict[str, Any]:
        import httpx

        token = self._token()
        target = chat_id or self.config.get("chat_id")
        if not token or not target:
            return {"success": False, "error": "telegram channel needs bot_token and chat_id"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target, "text": text},
            )
        ok = resp.status_code == 200
        return {"success": ok, "status_code": resp.status_code}


#: platform -> adapter. server.py validates a saved channel's platform
#: against these keys, so the names here are the supported set.
_ADAPTERS: dict[str, type[BaseAdapter]] = {
    "telegram": TelegramAdapter,
}


class ChannelManager:
    """Singleton owning every configured channel adapter."""

    _instance: "ChannelManager | None" = None

    def __init__(self) -> None:
        self._adapters: dict[str, BaseAdapter] = {}
        self._broadcast: Callable[[str, dict[str, Any]], Any] | None = None
        self._started = False

    @classmethod
    def get(cls) -> "ChannelManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_broadcast(self, fn: Callable[[str, dict[str, Any]], Any]) -> None:
        """Wire the harness's notification broadcaster for inbound messages."""
        self._broadcast = fn

    async def _load_rows(self) -> list[dict[str, Any]]:
        from python.harness import db

        try:
            rows = await db.fetch(
                "SELECT id, platform, name, enabled, config FROM chat_channels"
            )
        except Exception as exc:
            log.info("channels: no channel rows available (%s)", exc)
            return []

        out: list[dict[str, Any]] = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception:
                    cfg = {}
            out.append(
                {
                    "id": r["id"],
                    "platform": str(r["platform"] or "").lower(),
                    "name": r["name"],
                    "enabled": bool(r["enabled"]),
                    "config": cfg or {},
                }
            )
        return out

    async def _on_message(self, msg: dict[str, Any]) -> None:
        """Surface an inbound message to every connected UI client."""
        if not self._broadcast:
            return
        try:
            result = self._broadcast("channels.message", msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.warning("channels: broadcast failed: %s", exc)

    async def start(self) -> None:
        self._started = True
        await self.reload()

    async def reload(self) -> dict[str, Any]:
        """Stop every adapter and rebuild from the database."""
        for adapter in list(self._adapters.values()):
            await adapter.stop()
        self._adapters.clear()

        rows = await self._load_rows()
        started = 0
        for row in rows:
            cls = _ADAPTERS.get(row["platform"])
            if not cls:
                log.warning("channels: unknown platform %r — skipped", row["platform"])
                continue
            adapter = cls(row["id"], row["name"], row["config"])
            self._adapters[row["id"]] = adapter
            if row["enabled"] and self._started:
                await adapter.start(self._on_message)
                started += 1

        log.info("ChannelManager: %d channel(s) loaded, %d started", len(rows), started)
        return self.status()

    async def send(self, channel_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
        adapter = self._adapters.get(channel_id)
        if not adapter:
            return {"success": False, "error": f"channel {channel_id!r} is not configured"}
        return await adapter.send(text, **kwargs)

    def status(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "count": len(self._adapters),
            "channels": [a.status() for a in self._adapters.values()],
            "supported_platforms": sorted(_ADAPTERS),
        }

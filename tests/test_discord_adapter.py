"""
The Discord adapter (UI-084).

The settings panel offered Discord as its dropdown *default*, with accurate
setup instructions -- create an application, enable MESSAGE CONTENT INTENT,
invite with the bot scope -- and no adapter behind any of it. A user could
follow every step, paste a valid token, click Save, and get a channel the
manager had no class to run. It was the one place in the app where the
interface asserted something the code could not do.

Everything here runs without a Gateway connection. The socket is discord.py's
problem; the parts that are ours are which messages get answered and whether
policy is inherited rather than reimplemented, and both are testable offline.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from python.harness.channels import _ADAPTERS, BaseAdapter, DiscordAdapter


def _adapter(**config) -> DiscordAdapter:
    return DiscordAdapter("chan-1", "test discord", config)


def _msg(*, content="hello", author_id=2, bot=False, mentions=(), dm=False, name="steven"):
    channel_type = "DMChannel" if dm else "TextChannel"
    channel = type(channel_type, (), {})()
    channel.id = 4242
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, name=name, bot=bot),
        mentions=list(mentions),
        channel=channel,
    )


ME = SimpleNamespace(id=1, name="tala")


# --- registration ------------------------------------------------------------

def test_discord_is_a_real_platform_now():
    """The dropdown's default must resolve to something the manager can run."""
    assert _ADAPTERS.get("discord") is DiscordAdapter
    assert issubclass(DiscordAdapter, BaseAdapter)


# --- who gets answered -------------------------------------------------------

def test_a_direct_message_is_answered():
    record = _adapter()._as_record(_msg(dm=True), ME)
    assert record is not None
    assert record["text"] == "hello"
    assert record["platform"] == "discord"
    assert record["chat_id"] == 4242
    assert record["sender"] == "steven"


def test_a_mention_in_a_channel_is_answered():
    record = _adapter()._as_record(_msg(mentions=[ME]), ME)
    assert record is not None


def test_an_unaddressed_channel_message_is_ignored():
    assert _adapter()._as_record(_msg(), ME) is None


def test_respond_to_all_answers_unaddressed_messages():
    assert _adapter(respond_to_all=True)._as_record(_msg(), ME) is not None


def test_our_own_messages_are_never_answered():
    """Otherwise the bot talks to itself indefinitely."""
    assert _adapter(respond_to_all=True)._as_record(_msg(author_id=ME.id), ME) is None


def test_other_bots_are_never_answered():
    assert _adapter(respond_to_all=True)._as_record(_msg(bot=True), ME) is None


# --- the intent everyone forgets ---------------------------------------------

def test_empty_content_reports_the_missing_intent():
    """
    A bot with MESSAGE CONTENT INTENT off connects fine and receives every
    message with empty content. Connected-and-deaf looks exactly like
    nobody-is-talking-to-it, so it has to say so.
    """
    adapter = _adapter(respond_to_all=True)
    assert adapter._as_record(_msg(content="   ", dm=True), ME) is None
    assert "MESSAGE CONTENT INTENT" in (adapter.last_error or "")


# --- policy is inherited, not reimplemented ----------------------------------

def test_the_allowlist_is_the_inherited_one():
    """
    Fail closed, from BaseAdapter. An adapter that reimplemented this could
    quietly disagree with the other one; UI-083 was this check being collected
    and never read.
    """
    adapter = _adapter(allowlist="steven")
    assert adapter.is_allowed({"sender": "steven"}) is True
    assert adapter.is_allowed({"sender": "someone-else"}) is False
    assert _adapter().is_allowed({"sender": "steven"}) is False, "an empty allowlist permits nobody"


@pytest.mark.asyncio
async def test_poll_drains_what_the_gateway_delivered():
    """
    The Gateway pushes; the base loop pulls. Draining through poll() is what
    keeps the allowlist check, the rejection counter and the backoff in one
    place for both platforms.
    """
    adapter = _adapter()
    await adapter._inbox.put({"text": "one"})
    await adapter._inbox.put({"text": "two"})

    assert [m["text"] for m in await adapter.poll()] == ["one", "two"]
    assert await adapter.poll() == [], "a drained queue yields nothing on the next pass"


# --- transport ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_refuses_without_a_token_or_target():
    result = await _adapter().send("hi")
    assert result["success"] is False
    assert "bot_token" in result["error"]


@pytest.mark.asyncio
async def test_a_missing_token_is_an_error_not_a_crash():
    """A misconfigured channel is a status line, never a dead harness."""
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="bot_token"):
        await adapter._connect_once()


@pytest.mark.asyncio
async def test_the_gateway_loop_survives_a_failing_connection():
    adapter = _adapter(bot_token="")
    adapter._running = True

    async def _stop_soon():
        await asyncio.sleep(0.05)
        adapter._running = False

    await asyncio.gather(adapter._gateway_loop(), _stop_soon())

    assert adapter.last_error and "bot_token" in adapter.last_error


# --- the key the UI actually writes ------------------------------------------

@pytest.mark.parametrize("key", ["allowed_users", "allowlist", "allow_list"])
def test_every_spelling_of_the_allowlist_is_read(key):
    """
    The settings UI writes `allowed_users`; this read `allowlist` and
    `allow_list`. They never met, so the allowlist always parsed as empty --
    and because is_allowed fails closed, every inbound message on every channel
    was rejected regardless of what the user typed. A correctly configured
    Telegram bot ignored its own owner, and logged it as an authorisation
    decision rather than a key mismatch.
    """
    adapter = DiscordAdapter("c", "n", {key: "steven, @weavor"})
    assert adapter.allowlist() == {"steven", "weavor"}
    assert adapter.is_allowed({"sender": "steven"}) is True


def test_the_ui_payload_shape_authorises_its_own_user():
    """The exact dict renderer.js builds on save, end to end through the check."""
    from python.harness.channels import TelegramAdapter

    ui_config = {"bot_token": "t", "allowed_users": "steven", "respond_to_all": False}
    for cls in (DiscordAdapter, TelegramAdapter):
        adapter = cls("c", "n", dict(ui_config))
        assert adapter.is_allowed({"sender": "steven"}) is True, f"{cls.__name__} rejects its own user"
        assert adapter.is_allowed({"sender": "stranger"}) is False

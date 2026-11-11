"""v3.6.0: smoke-тест notify_achievement_granted — поведение no-op когда
канал не сконфигурен или бот не запущен.

Покрывает:
- channel_achievements_id не задан → return False, без вызова Discord API
- _bot_running=False → return False, без副作用
- некорректный channel_id (не цифра) → return False
- корректный ID + канал в кеше → вызывает channel.send(embed=...)

Импортирует bot как модуль; подменяет _bot_running / _get_bot / db.get_setting
через monkeypatch без старта discord client.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_notify_noop_when_channel_not_configured(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "_bot_running", True)

    class FakeBot:
        def get_channel(self, cid):
            return None

        async def fetch_channel(self, cid):
            raise AssertionError("should not fetch — channel_id is invalid")

    monkeypatch.setattr(bot, "_get_bot", lambda: FakeBot())

    async def fake_get_setting(key):
        assert key == "channel_achievements_id"
        return None

    import db
    monkeypatch.setattr(db, "get_setting", fake_get_setting)

    result = await bot.notify_achievement_granted(123, 456, {"name": "Test"})
    assert result is False


@pytest.mark.asyncio
async def test_notify_noop_when_channel_id_not_digit(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "_bot_running", True)

    class FakeBot:
        def get_channel(self, cid):
            raise AssertionError("should not even reach get_channel")

        async def fetch_channel(self, cid):
            raise AssertionError("should not reach fetch_channel")

    monkeypatch.setattr(bot, "_get_bot", lambda: FakeBot())

    async def fake_get_setting(key):
        return "abc-not-a-number"

    import db
    monkeypatch.setattr(db, "get_setting", fake_get_setting)

    result = await bot.notify_achievement_granted(123, 456, {"name": "Test"})
    assert result is False


@pytest.mark.asyncio
async def test_notify_noop_when_bot_not_running(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "_bot_running", False)

    # Даже если канал сконфигурен — _get_bot не должен вызываться
    monkeypatch.setattr(bot, "_get_bot", lambda: pytest.fail("should not call _get_bot"))

    result = await bot.notify_achievement_granted(123, 456, {"name": "Test"})
    assert result is False


@pytest.mark.asyncio
async def test_notify_sends_embed_when_channel_cached(monkeypatch):
    """Канал в кеше, send отрабатывает → True, и в send передаётся embed."""
    import bot
    import db

    monkeypatch.setattr(bot, "_bot_running", True)

    sent_calls = []

    class FakeChannel:
        def __init__(self):
            self.id = 999
            self.send_args = None

        async def send(self, embed=None):
            sent_calls.append(embed)
            return SimpleNamespace(id=1)

    fake_channel = FakeChannel()

    class FakeGuild:
        id = 123

        def get_member(self, uid):
            return None  # fallback на <@uid>

        async def fetch_member(self, uid):
            return None

    class FakeBot:
        def get_channel(self, cid):
            assert cid == 999
            return fake_channel

        async def fetch_channel(self, cid):
            raise AssertionError("should use cache, not fetch")

        def get_guild(self, gid):
            return FakeGuild()

    monkeypatch.setattr(bot, "_get_bot", lambda: FakeBot())

    async def fake_get_setting(key):
        return "999"

    monkeypatch.setattr(db, "get_setting", fake_get_setting)

    result = await bot.notify_achievement_granted(
        123,
        456,
        {
            "name": "  Первая цитата  ",
            "description": "  Описание ачивки  ",
            "icon_config": {"icon_url": "https://example.com/x.png"},
        },
    )
    assert result is True
    assert len(sent_calls) == 1
    embed = sent_calls[0]
    # Embed должен содержать наш title (с эмодзи 🏅 + именем)
    assert "Первая цитата" in str(embed.title)
    # icon_url должен дойти до thumbnail
    # discord.Embed хранит thumbnail как dict в ._thumbnail
    thumb = getattr(embed, "_thumbnail", None) or getattr(embed, "thumbnail", None)
    assert thumb is not None
    assert "example.com/x.png" in str(thumb)


@pytest.mark.asyncio
async def test_notify_returns_false_when_channel_send_raises(monkeypatch):
    """Send падает с discord.HTTPException → возвращаем False, не падаем наружу."""
    import bot
    import db
    import discord

    monkeypatch.setattr(bot, "_bot_running", True)

    class FakeChannel:
        async def send(self, embed=None):
            response = MagicMock(status=403, reason="Forbidden")
            raise discord.HTTPException(response=response, message="forbidden")

    class FakeBot:
        def get_channel(self, cid):
            return FakeChannel()

        async def fetch_channel(self, cid):
            return FakeChannel()

        def get_guild(self, gid):
            return None  # fallback на <@uid>

    monkeypatch.setattr(bot, "_get_bot", lambda: FakeBot())

    async def fake_get_setting(key):
        return "999"

    monkeypatch.setattr(db, "get_setting", fake_get_setting)

    result = await bot.notify_achievement_granted(123, 456, {"name": "Test"})
    assert result is False

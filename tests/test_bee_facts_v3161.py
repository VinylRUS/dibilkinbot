"""v3.16.1: тесты фильтра каналов пчелы + фактов о пчёлах.

Покрывает:
- _random_public_channel исключает служебные каналы (цитатник/анонсы);
- если все публичные каналы исключены → None (пчела не спавнится, не падает);
- _bee_excluded_channel_ids собирает ID из guild-настроек;
- BEE_FACTS грузится из static/bee_facts.json (непустой список);
- _bee_fact_suffix: формат приписки, пустая строка при p=0, факт при p=1.

Discord-объекты (guild/каналы/permissions) мокаются — живой бот не нужен.
Паттерн как в test_frames_v316.py: прямые вызовы по тестовой гильдии,
чистка настроек за собой.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import honey_cog
from honey_cog import HoneyCog
from conftest import TEST_GUILD_ID, run_async


def _fake_channel(ch_id: int, *, everyone_view: bool = True,
                  bot_send: bool = True) -> MagicMock:
    """Мок discord.TextChannel с управляемыми permissions_for."""
    ch = MagicMock()
    ch.id = ch_id

    everyone_perms = MagicMock()
    everyone_perms.view_channel = everyone_view
    bot_perms = MagicMock()
    bot_perms.send_messages = bot_send

    def _perms_for(role_or_member):
        # everyone == guild.default_role, bot == guild.me
        return bot_perms if role_or_member is _SENTINEL_ME else everyone_perms

    ch.permissions_for.side_effect = _perms_for
    return ch


# Маркеры, чтобы _perms_for различал default_role и me.
_SENTINEL_EVERYONE = object()
_SENTINEL_ME = object()


def _fake_guild(channels: list[MagicMock]) -> MagicMock:
    guild = MagicMock()
    guild.default_role = _SENTINEL_EVERYONE
    guild.me = _SENTINEL_ME
    guild.text_channels = channels
    return guild


def _cog() -> HoneyCog:
    """HoneyCog без запуска фоновых циклов (минимальный мок bot)."""
    cog = HoneyCog.__new__(HoneyCog)  # обходим __init__ (не стартуем loops)
    cog.bot = MagicMock()
    return cog


# ---------- Фильтр каналов ----------

def test_random_channel_excludes_service_channels():
    """Пчела не выбирает исключённые ID (цитатник/анонсы)."""
    quotes = _fake_channel(1001)
    updates = _fake_channel(1002)
    general = _fake_channel(2001)
    guild = _fake_guild([quotes, updates, general])

    cog = _cog()
    # 50 прогонов: общий канал — единственный допустимый, всегда он
    for _ in range(50):
        ch = cog._random_public_channel(guild, exclude_ids={1001, 1002})
        assert ch is general


def test_random_channel_none_when_all_excluded():
    """Все публичные каналы исключены → None (пчела не спавнится)."""
    quotes = _fake_channel(1001)
    updates = _fake_channel(1002)
    guild = _fake_guild([quotes, updates])

    cog = _cog()
    assert cog._random_public_channel(guild, exclude_ids={1001, 1002}) is None


def test_random_channel_skips_admin_and_no_send():
    """Приватные (everyone без view) и каналы без прав бота — мимо."""
    admin = _fake_channel(3001, everyone_view=False)
    muted = _fake_channel(3002, bot_send=False)
    general = _fake_channel(2001)
    guild = _fake_guild([admin, muted, general])

    cog = _cog()
    for _ in range(20):
        assert cog._random_public_channel(guild) is general


def test_excluded_channel_ids_from_settings(client):
    """_bee_excluded_channel_ids собирает ID из настроек гильдии."""
    import db

    async def _scenario():
        # Настроим два служебных канала, остальные ключи пустые.
        await db.g_set_setting(TEST_GUILD_ID, "channel_quotes_id", "555")
        await db.g_set_setting(TEST_GUILD_ID, "channel_updates_id", "666")
        try:
            cog = _cog()
            ids = await cog._bee_excluded_channel_ids(TEST_GUILD_ID)
            assert 555 in ids
            assert 666 in ids
        finally:
            await db.g_set_setting(TEST_GUILD_ID, "channel_quotes_id", None)
            await db.g_set_setting(TEST_GUILD_ID, "channel_updates_id", None)

    run_async(_scenario())


# ---------- Факты о пчёлах ----------

def test_bee_facts_loaded():
    """static/bee_facts.json загружен в непустой список строк."""
    assert isinstance(honey_cog.BEE_FACTS, list)
    assert len(honey_cog.BEE_FACTS) >= 40
    assert all(isinstance(f, str) and f.strip() for f in honey_cog.BEE_FACTS)


def test_fact_suffix_empty_when_probability_zero(monkeypatch):
    """При BEE_FACT_PROBABILITY=0 приписки нет."""
    monkeypatch.setattr(honey_cog, "BEE_FACT_PROBABILITY", 0.0)
    assert honey_cog._bee_fact_suffix() == ""


def test_fact_suffix_added_when_probability_one(monkeypatch):
    """При BEE_FACT_PROBABILITY=1 приписка содержит факт в нужном формате."""
    monkeypatch.setattr(honey_cog, "BEE_FACT_PROBABILITY", 1.0)
    suffix = honey_cog._bee_fact_suffix()
    assert suffix.startswith("\n\n> 💡 А пока ты реагируешь, знай: ")
    # факт из массива реально подставлен
    assert any(fact in suffix for fact in honey_cog.BEE_FACTS)


def test_fact_suffix_empty_when_no_facts(monkeypatch):
    """Пустой список фактов → всегда пустая приписка, даже при p=1."""
    monkeypatch.setattr(honey_cog, "BEE_FACTS", [])
    monkeypatch.setattr(honey_cog, "BEE_FACT_PROBABILITY", 1.0)
    assert honey_cog._bee_fact_suffix() == ""

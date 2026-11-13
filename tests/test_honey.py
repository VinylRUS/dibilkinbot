"""v3.11: тесты экономики «Мёда» 🍯.

Проверяем: заработок выводится из существующих таблиц (watched/ratings/quotes/
user_achievements), траты/бонусы идут через honey_ledger, баланс не уходит в минус,
лидерборд и стрик активности считаются корректно.

Паттерн как в conftest: прямые вставки в guild-таблицы тестовой гильдии.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
import pytest

from conftest import TEST_GUILD_ID, run_async

HONEY_USER = 90001  # уникальный id, не пересекается с тестовой Маткой (1001)


async def _seed():
    """Заполнить данные пользователя HONEY_USER и очистить предыдущие прогоны."""
    import db
    from config import settings
    from guild import guild_table

    t_watched = guild_table(TEST_GUILD_ID, "watched")
    t_ratings = guild_table(TEST_GUILD_ID, "ratings")
    t_quotes = guild_table(TEST_GUILD_ID, "quotes")
    t_uach = guild_table(TEST_GUILD_ID, "user_achievements")
    t_ledger = guild_table(TEST_GUILD_ID, "honey_ledger")
    t_act = guild_table(TEST_GUILD_ID, "activity_log")
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(settings.database_path) as c:
        # Идемпотентность: чистим всё, что относится к HONEY_USER
        await c.execute(f"DELETE FROM {t_watched} WHERE watcher_user_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_ratings} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_quotes} WHERE recorded_by = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_uach} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_ledger} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_act} WHERE actor_discord_id = ?", (HONEY_USER,))

        # 2 просмотра → 2 * 5 = 10
        for i in range(2):
            await c.execute(
                f"INSERT INTO {t_watched} (title, watched_at, watcher_user_id) VALUES (?, ?, ?)",
                (f"Film {i}", now, HONEY_USER))
        # 3 оценки → 3 * 3 = 9 (distinct winner_id из-за UNIQUE)
        for wid in range(1, 4):
            await c.execute(
                f"INSERT INTO {t_ratings} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid, HONEY_USER, 7.0, now))
        # 1 цитата → 1 * 2 = 2
        await c.execute(
            f"INSERT INTO {t_quotes} (author, text, recorded_by, recorded_at) VALUES (?, ?, ?, ?)",
            ("Someone", "quote text", HONEY_USER, now))
        # 1 активная ачивка → 1 * 25 = 25
        await c.execute(
            f"INSERT INTO {t_uach} (achievement_id, user_discord_id, granted_at, is_active) "
            f"VALUES (?, ?, ?, 1)", (1, HONEY_USER, now))
        await c.commit()
    # Итого earned = 10 + 9 + 2 + 25 = 46
    return 46


@pytest.mark.asyncio
async def test_honey_earned_balance(client):
    import db
    expected = await _seed()

    bd = await db.g_honey_breakdown(TEST_GUILD_ID, HONEY_USER)
    assert bd["watched"] == 2
    assert bd["ratings"] == 3
    assert bd["quotes"] == 1
    assert bd["achievements"] == 1
    assert bd["earned"] == expected
    assert bd["adjustments"] == 0
    assert bd["balance"] == expected
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == expected


@pytest.mark.asyncio
async def test_honey_spend_and_grant(client):
    import db
    earned = await _seed()  # 46

    # Трата больше баланса — отказ
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, earned + 1, "too much") is False
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned

    # Валидная трата
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, 20, "accent color") is True
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20

    # amount <= 0 — отказ, баланс не меняется
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, 0, "noop") is False
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20

    # Ручной бонус
    await db.g_honey_grant(TEST_GUILD_ID, HONEY_USER, 10, "event prize", granted_by=1001)
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20 + 10


@pytest.mark.asyncio
async def test_honey_balance_never_negative(client):
    import db
    await _seed()
    # Админ списывает больше, чем заработано (штраф) — баланс клампится в 0
    await db.g_honey_grant(TEST_GUILD_ID, HONEY_USER, -9999, "big penalty", granted_by=1001)
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == 0


@pytest.mark.asyncio
async def test_honey_leaderboard(client):
    import db
    earned = await _seed()
    board = await db.g_honey_leaderboard(TEST_GUILD_ID, limit=10)
    entry = next((r for r in board if r["user_discord_id"] == HONEY_USER), None)
    assert entry is not None
    assert entry["balance"] == earned


def test_dashboard_renders_honey(client):
    """Дашборд отдаёт 200 и содержит блок «Мёд» (контекст honey прокинут в шаблон)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Мёд" in r.text
    assert "honey-board" in r.text


@pytest.mark.asyncio
async def test_controversial_films(client):
    """«Спорные фильмы»: фильм с большим разбросом оценок ранжируется выше."""
    import db
    from config import settings
    from guild import guild_table

    t_win = guild_table(TEST_GUILD_ID, "winners")
    t_rat = guild_table(TEST_GUILD_ID, "ratings")
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(settings.database_path) as c:
        cur = await c.execute(
            f"INSERT INTO {t_win} (lot_name, confidence, detected_at) VALUES (?, ?, ?)",
            ("Спорный", "high", now))
        wid_spread = cur.lastrowid
        cur = await c.execute(
            f"INSERT INTO {t_win} (lot_name, confidence, detected_at) VALUES (?, ?, ?)",
            ("Единодушный", "high", now))
        wid_tight = cur.lastrowid
        # Большой разброс: 1,10,1,10 (σ=4.5)
        for i, val in enumerate((1.0, 10.0, 1.0, 10.0)):
            await c.execute(
                f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid_spread, 80000 + i, val, now))
        # Единодушие: 7,7,8,7 (σ≈0.43)
        for i, val in enumerate((7.0, 7.0, 8.0, 7.0)):
            await c.execute(
                f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid_tight, 81000 + i, val, now))
        await c.commit()

    rows = await db.g_controversial_films(TEST_GUILD_ID, min_ratings=3, limit=5)
    assert len(rows) == 2
    assert rows[0]["winner_id"] == wid_spread  # спорный — первый
    assert rows[0]["stddev"] > rows[1]["stddev"]
    assert rows[0]["count"] == 4


def test_profile_renders_honey(client):
    """Публичный профиль отдаёт 200 и показывает карточку «Мёд»."""
    r = client.get("/u/1001")  # TEST_ADMIN_DISCORD_ID из conftest
    assert r.status_code == 200
    assert "Мёд" in r.text


@pytest.mark.asyncio
async def test_activity_streak(client):
    import db
    from config import settings
    from guild import guild_table

    await _seed()  # _seed чистит activity_log пользователя
    t_act = guild_table(TEST_GUILD_ID, "activity_log")
    today = datetime.utcnow().date()
    async with aiosqlite.connect(settings.database_path) as c:
        for d in (today, today - timedelta(days=1), today - timedelta(days=2)):
            await c.execute(
                f"INSERT INTO {t_act} (event_type, actor_discord_id, actor_name, created_at) "
                f"VALUES (?, ?, ?, ?)",
                ("login", HONEY_USER, "tester", d.isoformat() + "T12:00:00"))
        await c.commit()
    # 3 дня подряд, заканчивая сегодня
    assert await db.g_activity_streak(TEST_GUILD_ID, HONEY_USER) == 3

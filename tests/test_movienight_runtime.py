"""v3.11: тесты DB-слоя Discord-рантайм фич киновечера (RSVP, countdown, авто-промпт).

Сами Discord-хендлеры/циклы проверяются на живом боте; здесь — проверяемая
часть: хранение RSVP и выборки «пора отправить countdown / промпт оценок».
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
import pytest

from conftest import TEST_GUILD_ID, run_async


async def _clean():
    import db  # noqa
    from config import settings
    from guild import guild_table
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(f"DELETE FROM {guild_table(TEST_GUILD_ID, 'movie_nights')}")
        await c.execute(f"DELETE FROM {guild_table(TEST_GUILD_ID, 'movie_night_rsvp')}")
        await c.commit()


async def _add_movie_night(scheduled_at: datetime, event_id: int) -> int:
    from config import settings
    from guild import guild_table
    t = guild_table(TEST_GUILD_ID, "movie_nights")
    async with aiosqlite.connect(settings.database_path) as c:
        cur = await c.execute(
            f"INSERT INTO {t} (title, scheduled_at, created_by, created_at, event_id) "
            f"VALUES (?, ?, ?, ?, ?)",
            ("Фильм", scheduled_at.isoformat(), 1001, datetime.utcnow().isoformat(), event_id))
        await c.commit()
        return cur.lastrowid


@pytest.mark.asyncio
async def test_rsvp(client):
    import db
    await _clean()
    EV = 55501
    await db.g_add_movie_night_rsvp(TEST_GUILD_ID, EV, 1)
    await db.g_add_movie_night_rsvp(TEST_GUILD_ID, EV, 2)
    await db.g_add_movie_night_rsvp(TEST_GUILD_ID, EV, 2)  # дубль игнорируется
    assert await db.g_count_movie_night_rsvp(TEST_GUILD_ID, EV) == 2
    assert set(await db.g_list_movie_night_rsvp(TEST_GUILD_ID, EV)) == {1, 2}
    await db.g_remove_movie_night_rsvp(TEST_GUILD_ID, EV, 1)
    assert await db.g_count_movie_night_rsvp(TEST_GUILD_ID, EV) == 1


@pytest.mark.asyncio
async def test_due_countdown(client):
    import db
    await _clean()
    now = datetime.utcnow()
    mn = await _add_movie_night(now - timedelta(minutes=1), event_id=900)  # только что начался
    await _add_movie_night(now + timedelta(hours=5), event_id=901)          # ещё не скоро

    due = await db.g_movie_nights_due_countdown(
        TEST_GUILD_ID, now.isoformat(), (now - timedelta(hours=1)).isoformat())
    ids = [d["id"] for d in due]
    assert mn in ids
    assert len(due) == 1  # будущий не попал

    await db.g_mark_movie_night_flag(TEST_GUILD_ID, mn, "countdown_sent")
    due2 = await db.g_movie_nights_due_countdown(
        TEST_GUILD_ID, now.isoformat(), (now - timedelta(hours=1)).isoformat())
    assert mn not in [d["id"] for d in due2]


@pytest.mark.asyncio
async def test_due_rate_prompt(client):
    import db
    await _clean()
    now = datetime.utcnow()
    mn = await _add_movie_night(now - timedelta(hours=4), event_id=910)  # прошёл 4ч назад
    await _add_movie_night(now - timedelta(minutes=30), event_id=911)    # ещё идёт/только что

    cutoff = (now - timedelta(hours=3)).isoformat()
    floor = (now - timedelta(days=2)).isoformat()
    due = await db.g_movie_nights_due_rate(TEST_GUILD_ID, cutoff, floor)
    assert [d["id"] for d in due] == [mn]

    await db.g_mark_movie_night_flag(TEST_GUILD_ID, mn, "rate_prompt_sent")
    due2 = await db.g_movie_nights_due_rate(TEST_GUILD_ID, cutoff, floor)
    assert mn not in [d["id"] for d in due2]


@pytest.mark.asyncio
async def test_mark_flag_whitelist(client):
    import db
    with pytest.raises(ValueError):
        await db.g_mark_movie_night_flag(TEST_GUILD_ID, 1, "scheduled_at")

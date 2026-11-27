"""v3.15.1: тесты перестановки «Мёда» и сборов фильмов.

Покрывает:
- окно приёма ставок: только до первой крутки текущей загрузки колеса
  (he_bets_round_open + /api/honey/bets/place);
- g_get_last_real_winner — маркер окна (игнорирует тестовых победителей);
- g_honey_ledger_recent — последние операции леджера;
- сбор фильмов: добавление фильма во время сбора (POST /api/collection/add_film),
  авто-вишлист, пометка просмотренным при появлении колеса (start_spin),
  исчезновение просмотренного из вишлиста через неделю (g_watchlist_cleanup).

Паттерн как в test_honey_v315.py: клиент — сессия Матки (conftest),
прямые вставки/вызовы по тестовой гильдии, чистка за собой.
"""
from __future__ import annotations

import aiosqlite
from datetime import datetime, timedelta

import pytest

from conftest import TEST_GUILD_ID, run_async

# Уникальный юзер для леджера (не пересекается с v3.11 90001 и v3.15 91001-91004)
LEDGER_UID = 91005


async def _purge():
    """Идемпотентная очистка honey/wheel/watchlist-таблиц тестовой гильдии."""
    import guild as guild_mod
    from config import settings

    bases = ["honey_bets", "honey_ledger", "watchlist", "wheel_items", "winners",
             "collections", "collection_participants", "collection_picks"]
    async with aiosqlite.connect(settings.database_path) as c:
        for base in bases:
            t = guild_mod.guild_table(TEST_GUILD_ID, base)
            await c.execute(f"DELETE FROM {t}")
        await c.commit()


# ===================== v3.15.1: окно ставок (до первой крутки) =====================

@pytest.mark.asyncio
async def test_bets_round_open_window(client):
    """Ставки открыты до победителя текущей загрузки; новая загрузка
    (лоты новее победителя) снова открывает окно."""
    import db
    import honey_economy as he
    await _purge()

    # 1. Пустое колесо, победителей нет → открыто
    opened, reason = await he.he_bets_round_open(TEST_GUILD_ID)
    assert opened, reason

    # 2. Загрузили лоты (сбор завершился) — победителя ещё не было → открыто
    await db.g_add_wheel_item(TEST_GUILD_ID, "Фильм А", None, 1001)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Фильм Б", None, 1001)
    opened, _ = await he.he_bets_round_open(TEST_GUILD_ID)
    assert opened

    # 3. Появился реальный победитель (детект новее загрузки лотов) → закрыто
    await db.g_add_winner(TEST_GUILD_ID, "1", "Фильм А", None, is_test=False)
    opened, reason = await he.he_bets_round_open(TEST_GUILD_ID)
    assert not opened
    assert "крутка" in reason

    # 4. Тестовый победитель — не считается (is_test=1 игнорируется):
    #    окно по-прежнему закрыто только из-за реального победителя выше.
    await db.g_add_winner(TEST_GUILD_ID, "2", "Фильм Б", None, is_test=True)
    opened, _ = await he.he_bets_round_open(TEST_GUILD_ID)
    assert not opened  # реальный «Фильм А» всё ещё новее загрузки

    # 5. Новая загрузка колеса (лоты новее победителя) → окно снова открыто
    await db.g_clear_wheel(TEST_GUILD_ID)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Новый фильм В", None, 1001)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Новый фильм Г", None, 1001)
    opened, _ = await he.he_bets_round_open(TEST_GUILD_ID)
    assert opened

    await _purge()


@pytest.mark.asyncio
async def test_bets_place_closed_after_first_spin(client):
    """POST /api/honey/bets/place: после победителя текущей загрузки — 409
    bets_closed (раньше окно снова открывалось через 60 минут)."""
    import db
    import honey_economy as he
    await _purge()

    # Финансируем Матку для ставок
    await he._ledger_insert(TEST_GUILD_ID, 1001, 50, reason="test_seed_v3151")

    await db.g_add_wheel_item(TEST_GUILD_ID, "Ставочный А", None, 1001)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Ставочный Б", None, 1001)
    # Победитель уже определён ПОСЛЕ текущей загрузки → окно закрыто
    await db.g_add_winner(TEST_GUILD_ID, "1", "Ставочный А", None, is_test=False)

    r = client.post("/api/honey/bets/place",
                    data={"item_name": "Ставочный Б", "amount": "5"})
    assert r.status_code == 409
    d = r.json()
    assert d.get("error_code") in ("bets_closed", "winner_recent")

    # Новая загрузка лотов — окно снова открыто, ставка проходит
    await db.g_clear_wheel(TEST_GUILD_ID)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Свежий А", None, 1001)
    await db.g_add_wheel_item(TEST_GUILD_ID, "Свежий Б", None, 1001)
    r = client.post("/api/honey/bets/place",
                    data={"item_name": "Свежий А", "amount": "5"})
    assert r.status_code == 200
    assert r.json().get("ok")

    await _purge()


# ===================== v3.15.1: маркер последнего победителя =====================

@pytest.mark.asyncio
async def test_get_last_real_winner_ignores_test(client):
    import db
    await _purge()

    assert await db.g_get_last_real_winner(TEST_GUILD_ID) is None

    await db.g_add_winner(TEST_GUILD_ID, "1", "Тестовый", None, is_test=True)
    assert await db.g_get_last_real_winner(TEST_GUILD_ID) is None

    await db.g_add_winner(TEST_GUILD_ID, "2", "Реальный", None, is_test=False)
    w = await db.g_get_last_real_winner(TEST_GUILD_ID)
    assert w is not None and w["lot_name"] == "Реальный" and w["detected_at"]

    await _purge()


# ===================== v3.15.1: последние операции леджера =====================

@pytest.mark.asyncio
async def test_honey_ledger_recent(client):
    import db
    import honey_economy as he
    await _purge()

    await he._ledger_insert(TEST_GUILD_ID, LEDGER_UID, 10, reason="a")
    await he._ledger_insert(TEST_GUILD_ID, LEDGER_UID, -3, reason="b")
    await he._ledger_insert(TEST_GUILD_ID, LEDGER_UID, 7, reason="c")

    rows = await db.g_honey_ledger_recent(TEST_GUILD_ID, LEDGER_UID, limit=10)
    assert len(rows) == 3
    deltas = [r["delta"] for r in rows]
    assert 7 in deltas and -3 in deltas and 10 in deltas
    assert all(r["reason"] for r in rows)
    assert all(r["created_at"] for r in rows)

    await _purge()


# ===================== v3.15.1: сбор фильмов — полный жизненный цикл =====================

@pytest.mark.asyncio
async def test_collection_add_film_flow(client):
    """Фильм добавлен во время сбора → авто-вишлист → при появлении колеса
    помечен просмотренным → через неделю исчезает из вишлиста."""
    import db
    await _purge()

    # Без активного сбора добавить нельзя
    r = client.post("/api/collection/add_film", data={"title": "Без сбора"})
    assert r.status_code == 400
    assert r.json().get("error") == "no_active_collection"

    # Запускаем сбор от имени Матки
    coll = await db.g_start_collection(TEST_GUILD_ID, 1001, max_per_user=1, mode="wheel")
    assert coll and coll["id"]

    # Добавляем фильм во время сбора — автоматически попадает в вишлист
    title = "Тестовый фильм 3151"
    r = client.post("/api/collection/add_film", data={"title": title})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") and d.get("watchlist_id") and d.get("title") == title

    # Повторное добавление того же фильма тем же юзером — already_yours, без дубля
    r2 = client.post("/api/collection/add_film", data={"title": title})
    assert r2.status_code == 200 and r2.json().get("note") == "already_yours"

    # В вишлисте запись одна и непросмотренная
    wl = await db.g_list_watchlist(TEST_GUILD_ID, 1001, include_watched=True)
    mine = [w for w in wl if w[1] == title]
    assert len(mine) == 1 and mine[0][4] == 0

    # Выбираем фильм в сбор (ровно max_per_user=1)
    r = client.post("/api/collection/save_picks",
                    data={"watchlist_ids": str(d["watchlist_id"])})
    assert r.status_code == 200 and r.json().get("ok")

    # Завершаем сбор — колесо появляется, фильмы помечаются просмотренными
    r = client.post("/api/collection/start_spin", data={"force": "false"})
    assert r.status_code == 200
    spin = r.json()
    assert spin.get("ok") and spin.get("wheel_count") == 1

    wl = await db.g_list_watchlist(TEST_GUILD_ID, 1001, include_watched=True)
    mine = [w for w in wl if w[1] == title]
    assert len(mine) == 1 and mine[0][4] == 1  # is_watched=1 — колесо появилось

    # Через неделю просмотренный исчезает: сдвигаем watched_at на 8 дней назад
    import guild as guild_mod
    from config import settings
    t_wl = guild_mod.guild_table(TEST_GUILD_ID, "watchlist")
    old_date = (datetime.utcnow() - timedelta(days=8)).isoformat()
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"UPDATE {t_wl} SET watched_at = ? WHERE title = ?", (old_date, title))
        await c.commit()

    removed = await db.g_watchlist_cleanup(TEST_GUILD_ID)
    assert removed == 1

    wl = await db.g_list_watchlist(TEST_GUILD_ID, 1001, include_watched=True)
    assert not [w for w in wl if w[1] == title]  # исчез совсем

    await _purge()


@pytest.mark.asyncio
async def test_watchlist_cleanup_keeps_fresh(client):
    """Чистка удаляет только просмотренное старше 7 дней — свежее и
    непросмотренное остаются."""
    import db
    import guild as guild_mod
    from config import settings

    await _purge()
    t_wl = guild_mod.guild_table(TEST_GUILD_ID, "watchlist")
    now = datetime.utcnow()
    rows = [
        # (title, is_watched, watched_at)
        ("свежий просмотренный", 1, (now - timedelta(days=1)).isoformat()),
        ("старый просмотренный", 1, (now - timedelta(days=10)).isoformat()),
        ("непросмотренный", 0, None),
    ]
    async with aiosqlite.connect(settings.database_path) as c:
        for title, is_w, watched_at in rows:
            await c.execute(
                f"INSERT INTO {t_wl} (user_discord_id, title, tmdb_id, added_at, "
                f"is_watched, watched_at) VALUES (?, ?, NULL, ?, ?, ?)",
                (1001, title, now.isoformat(), is_w, watched_at))
        await c.commit()

    removed = await db.g_watchlist_cleanup(TEST_GUILD_ID)
    assert removed == 1  # только «старый просмотренный»

    left = await db.g_list_watchlist(TEST_GUILD_ID, 1001, include_watched=True)
    titles = [w[1] for w in left]
    assert "свежий просмотренный" in titles
    assert "непросмотренный" in titles
    assert "старый просмотренный" not in titles

    await _purge()

"""v3.16.0: тесты рамок аватара (надеть/снять) и ручного запуска пчелы.

Покрывает:
- g_frame_set / g_frame_get — надеть/снять/пере-надеть, максимум одна рамка;
- he_frame_equip — гард владения (not_owned), неизвестный ключ, 'none' = снять;
- he_frames_catalog — флаг equipped на надетой рамке;
- POST /api/honey/frame_equip/{key} — happy path + гард через API;
- публичный профиль /u/{id} — рендер класса frame-{key}, отсутствие класса
  без рамки;
- POST /api/honey/bee/launch — 503 без живого бота (роут смонтирован, гард
  superuser работает).

Паттерн как в test_honey_v315.py: клиент — сессия Матки (conftest),
прямые вставки/вызовы по тестовой гильдии, чистка за собой.
"""
from __future__ import annotations

import aiosqlite

import pytest

from conftest import TEST_GUILD_ID, TEST_ADMIN_DISCORD_ID, run_async

# Уникальный юзер (не пересекается с v3.15 91001-91004 и v3.15.1 91005)
FR_UID = 91006

# Все ключи каталога (FRAMES + GIF_FRAMES)
ALL_KEYS = ("pasechnik", "keeper", "night_bee", "medovik", "bee_mechanic")


async def _purge():
    """Идемпотентная очистка таблиц рамок тестовой гильдии."""
    import guild as guild_mod
    from config import settings

    # honey_bee_rounds не имеет колонки user_discord_id (раунды серверные)
    by_user = ["honey_user_frame", "user_cosmetics", "honey_bee_claims"]
    async with aiosqlite.connect(settings.database_path) as c:
        for base in by_user:
            t = guild_mod.guild_table(TEST_GUILD_ID, base)
            await c.execute(f"DELETE FROM {t} WHERE user_discord_id = ?", (FR_UID,))
        await c.commit()


async def _upsert_user():
    """Юзер нужен в users, иначе /u/{id} отдаёт 404 (не найден)."""
    import db as db_mod
    await db_mod.upsert_user(FR_UID, username=f"u{FR_UID}", display_name=f"U{FR_UID}")


async def _own(key: str, cosmetic_type: str = "frame"):
    """Дать юзеру владение косметикой напрямую (без трат мёда)."""
    import guild as guild_mod
    from config import settings
    from datetime import datetime

    t = guild_mod.guild_table(TEST_GUILD_ID, "user_cosmetics")
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"INSERT OR IGNORE INTO {t} (user_discord_id, cosmetic_type, "
            f"cosmetic_value, acquired_at) VALUES (?, ?, ?, ?)",
            (FR_UID, cosmetic_type, key, datetime.utcnow().isoformat()))
        await c.commit()


# ===================== БД: надеть/снять =====================

@pytest.mark.asyncio
async def test_frame_set_get_roundtrip(client):
    """Надеть → читается; пере-надеть → заменяется; None → снята."""
    import db
    await _purge()
    try:
        assert await db.g_frame_get(TEST_GUILD_ID, FR_UID) is None
        await db.g_frame_set(TEST_GUILD_ID, FR_UID, "keeper")
        assert await db.g_frame_get(TEST_GUILD_ID, FR_UID) == "keeper"
        await db.g_frame_set(TEST_GUILD_ID, FR_UID, "pasechnik")  # замена
        assert await db.g_frame_get(TEST_GUILD_ID, FR_UID) == "pasechnik"
        await db.g_frame_set(TEST_GUILD_ID, FR_UID, None)         # снять
        assert await db.g_frame_get(TEST_GUILD_ID, FR_UID) is None
    finally:
        await _purge()


# ===================== Экономика: эквип =====================

@pytest.mark.asyncio
async def test_frame_equip_guards_and_happy(client):
    import honey_economy as he
    await _purge()
    try:
        # 1. Не владеет — not_owned
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, "pasechnik")
        assert not r["ok"] and r["error"] == "not_owned"
        cat0 = await he.he_frames_catalog(TEST_GUILD_ID, FR_UID)
        assert cat0["equipped"] is None

        # 2. Неизвестный ключ
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, "nonexistent")
        assert not r["ok"] and r["error"] == "unknown_frame"

        # 3. Владеет → надета
        await _own("pasechnik")
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, "pasechnik")
        assert r["ok"] and r["equipped"] == "pasechnik"

        # 4. Каталог: equipped=True на пасечнике, False на остальных
        cat = await he.he_frames_catalog(TEST_GUILD_ID, FR_UID)
        assert cat["equipped"] == "pasechnik"
        by_key = {f["key"]: f for f in cat["frames"]}
        assert by_key["pasechnik"]["equipped"] is True
        assert by_key["keeper"]["equipped"] is False

        # 5. Снять (None) → equipped=None в каталоге
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, None)
        assert r["ok"] and r["equipped"] is None
        cat = await he.he_frames_catalog(TEST_GUILD_ID, FR_UID)
        assert cat["equipped"] is None
        assert all(f["equipped"] is False for f in cat["frames"])
    finally:
        await _purge()


@pytest.mark.asyncio
async def test_frame_equip_gif_frame_requires_gif_ownership(client):
    """GIF-рамка требует владения типом gif_frame (не frame)."""
    import honey_economy as he
    await _purge()
    try:
        # владеет статичной рамкой, но не GIF-рамкой
        await _own("pasechnik")
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, "bee_mechanic")
        assert not r["ok"] and r["error"] == "not_owned"
        # купим GIF-владение → надевается
        await _own("bee_mechanic", "gif_frame")
        r = await he.he_frame_equip(TEST_GUILD_ID, FR_UID, "bee_mechanic")
        assert r["ok"] and r["equipped"] == "bee_mechanic"
    finally:
        await _purge()


# ===================== API: эквип =====================

def test_frame_equip_api(client):
    import db
    import honey_economy as he
    run_async(_purge())
    try:
        # Матка из conftest владеет... ничего не владеет — гард
        r = client.post("/api/honey/frame_equip/keeper")
        assert r.status_code == 400
        assert r.json()["error"] == "not_owned"

        # даём владение Матке напрямую, надеваем через API
        run_async(_grant_admin_frame("keeper"))
        r = client.post("/api/honey/frame_equip/keeper")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "equipped": "keeper"}
        assert run_async(db.g_frame_get(TEST_GUILD_ID, TEST_ADMIN_DISCORD_ID)) == "keeper"

        # 'none' — снять
        r = client.post("/api/honey/frame_equip/none")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "equipped": None}
        assert run_async(db.g_frame_get(TEST_GUILD_ID, TEST_ADMIN_DISCORD_ID)) is None
    finally:
        run_async(_purge())
        run_async(_purge_admin_frame())


async def _grant_admin_frame(key: str):
    """Дать тестовой Матке владение рамкой (прямая вставка)."""
    import guild as guild_mod
    from config import settings
    from datetime import datetime

    t = guild_mod.guild_table(TEST_GUILD_ID, "user_cosmetics")
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"INSERT OR IGNORE INTO {t} (user_discord_id, cosmetic_type, "
            f"cosmetic_value, acquired_at) VALUES (?, 'frame', ?, ?)",
            (TEST_ADMIN_DISCORD_ID, key, datetime.utcnow().isoformat()))
        await c.commit()


async def _purge_admin_frame():
    """Чистка за Маткой: косметика + рамка + леджер"""
    import guild as guild_mod
    from config import settings

    tables = ["user_cosmetics", "honey_user_frame", "honey_ledger"]
    async with aiosqlite.connect(settings.database_path) as c:
        for base in tables:
            t = guild_mod.guild_table(TEST_GUILD_ID, base)
            await c.execute(f"DELETE FROM {t} WHERE user_discord_id = ?",
                            (TEST_ADMIN_DISCORD_ID,))
        await c.commit()


# ===================== Публичный профиль: рендер =====================

def test_public_profile_renders_frame_class(client):
    """/u/{id} отдаёт frame-{key} на аватаре, когда рамка надета,
    и не отдаёт классов рамок, когда снята."""
    import db
    run_async(_purge())
    run_async(_upsert_user())
    try:
        # без рамки — никаких frame-классов на аватаре
        r = client.get(f"/u/{FR_UID}")
        assert r.status_code == 200
        assert "frame-pasechnik" not in r.text
        assert "profile-avatar-wrap avatar-wrap lg" in r.text

        # надета — класс на wrap'е
        run_async(db.g_frame_set(TEST_GUILD_ID, FR_UID, "pasechnik"))
        r = client.get(f"/u/{FR_UID}")
        assert r.status_code == 200
        assert 'profile-avatar-wrap avatar-wrap lg frame-pasechnik' in r.text
    finally:
        run_async(_purge())


def test_public_profile_renders_gif_frame_class(client):
    import db
    run_async(_purge())
    run_async(_upsert_user())
    try:
        run_async(db.g_frame_set(TEST_GUILD_ID, FR_UID, "bee_mechanic"))
        r = client.get(f"/u/{FR_UID}")
        assert r.status_code == 200
        assert 'frame-bee_mechanic' in r.text
    finally:
        run_async(_purge())


# ===================== Ручной запуск пчелы =====================

def test_bee_manual_launch_bot_not_ready(client):
    """Без живого процесса бота роут отвечает 503 (гард bot not ready).

    Пчела «вылетает» в Discord-канал — в тестовом окружении бота нет,
    поэтому тестируем отказ; сам постинг покрывается экономикой
    (he_bee_round_create / he_bee_claim) в test_honey_v315.py.
    """
    r = client.post("/api/honey/bee/launch")
    assert r.status_code == 503
    assert "not ready" in r.json()["error"]


@pytest.mark.asyncio
async def test_bee_has_open_helper(client):
    """he_bee_has_open: False без раундов, True с открытым раундом."""
    import honey_economy as he
    import guild as guild_mod
    from config import settings
    from datetime import datetime

    await _purge()
    t = guild_mod.guild_table(TEST_GUILD_ID, "honey_bee_rounds")
    try:
        assert await he.he_bee_has_open(TEST_GUILD_ID) is False
        async with aiosqlite.connect(settings.database_path) as c:
            await c.execute(
                f"INSERT INTO {t} (channel_id, message_id, status, created_at) "
                f"VALUES (?, ?, 'open', ?)",
                (111, 222, datetime.utcnow().isoformat()))
            await c.commit()
        assert await he.he_bee_has_open(TEST_GUILD_ID) is True
    finally:
        # открытое состояние мешает другим тестам (bee_already_flying в кулдаунах)
        async with aiosqlite.connect(settings.database_path) as c:
            await c.execute(f"DELETE FROM {t}")
            await c.commit()

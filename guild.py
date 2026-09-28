"""
Multi-tenant: управление guild-specific таблицами.

Каждый Discord-сервер (guild) получает свой набор таблиц с префиксом guild_{id}_.
Глобальные таблицы (users, tg_links, movie_meta, settings для токенов) — общие.

Таблица `guilds` хранит список всех серверов с флагом approved.
Данные появятся в веб-панели только после апрува админом.
"""
from __future__ import annotations

import aiosqlite
import logging
from datetime import datetime
from typing import Optional

from config import settings

log = logging.getLogger("guild")

# Суффиксы guild-таблиц — единый контракт
GUILD_TABLES = ["watched", "quotes", "winners", "wheel_items", "ratings", "movie_nights", "settings", "filmnights"]


def validate_guild_id(guild_id: int | str) -> int:
    """Валидация guild_id — должен быть положительным числом.
    Бросает ValueError если невалидный (защита от SQL injection через имя таблицы).
    """
    gid = int(guild_id)
    if gid < 0:
        raise ValueError(f"guild_id must be >= 0, got {gid}")
    return gid


def guild_table(guild_id: int | str, base: str) -> str:
    """Имя guild-специфичной таблицы. Например guild_table(123, 'watched') → 'guild_123_watched'.
    base должен быть из GUILD_TABLES — иначе ValueError.
    """
    gid = validate_guild_id(guild_id)
    if base not in GUILD_TABLES:
        raise ValueError(f"Unknown guild table base: {base!r}. Must be one of {GUILD_TABLES}")
    return f"guild_{gid}_{base}"


def _connect():
    return aiosqlite.connect(settings.database_path)


# === Схема для guild_{id}_* таблиц ===
# Идентична схеме существующих глобальных таблиц, но без UNIQUE на title
# (на разных серверах могут смотреть одинаковые фильмы)

GUILD_SCHEMA_TEMPLATES = {
    "watched": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            watched_at TEXT NOT NULL,
            rating INTEGER,
            watcher_user_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_watched_at ON {table}(watched_at DESC);
    """,
    "quotes": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            author_user_id INTEGER,
            author_avatar_url TEXT,
            text TEXT NOT NULL,
            recorded_by INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            message_link TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_recorded_at ON {table}(recorded_at DESC);
    """,
    "winners": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id TEXT,
            lot_name TEXT NOT NULL,
            tmdb_id INTEGER,
            confidence TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            confirmed_at TEXT,
            confirmed_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_detected ON {table}(detected_at DESC);
    """,
    "wheel_items": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tmdb_id INTEGER,
            color TEXT,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_active ON {table}(is_active);
    """,
    "ratings": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            winner_id INTEGER NOT NULL,
            user_discord_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            UNIQUE(winner_id, user_discord_id)
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_winner ON {table}(winner_id);
    """,
    "movie_nights": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            scheduled_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            event_id INTEGER
        );
    """,
    "settings": """
        CREATE TABLE IF NOT EXISTS {table} (
            key TEXT PRIMARY KEY,
            value TEXT,
            is_secret INTEGER DEFAULT 0
        );
    """,
    "filmnights": """
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'active',
            max_per_user INTEGER NOT NULL DEFAULT 3,
            started_by INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            completed_by INTEGER,
            wheel_items_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status);
    """,
}


async def init_guild_tables(guild_id: int | str) -> None:
    """Создать все guild-специфичные таблицы для данного сервера.
    Безопасно вызывать многократно (CREATE TABLE IF NOT EXISTS).
    """
    gid = validate_guild_id(guild_id)
    async with _connect() as db:
        for base in GUILD_TABLES:
            table = guild_table(gid, base)
            schema = GUILD_SCHEMA_TEMPLATES[base].format(table=table)
            await db.executescript(schema)
        await db.commit()
    log.info("Initialized guild tables for guild_id=%s", gid)


async def drop_guild_tables(guild_id: int | str) -> None:
    """Удалить все guild-таблицы (используется при удалении бота с сервера).
    ВАЖНО: только для админа, irreversible.
    """
    gid = validate_guild_id(guild_id)
    async with _connect() as db:
        for base in GUILD_TABLES:
            table = guild_table(gid, base)
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        await db.commit()
    log.warning("Dropped guild tables for guild_id=%s", gid)


# === Глобальная таблица `guilds` ===

GUILD_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    icon_url TEXT,
    owner_id INTEGER,
    member_count INTEGER DEFAULT 0,
    approved INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by INTEGER
);
"""


async def init_guild_registry() -> None:
    """Создать таблицу `guilds` если её нет."""
    async with _connect() as db:
        await db.executescript(GUILD_REGISTRY_SCHEMA)
        await db.commit()


async def upsert_guild(
    guild_id: int,
    name: str,
    icon_url: str | None = None,
    owner_id: int | None = None,
    member_count: int = 0,
    auto_approve: bool = False,
) -> bool:
    """Зарегистрировать или обновить guild. Возвращает True если создан новый.
    Если уже существует — обновляет name/icon/owner/member_count.
    Если auto_approve=True — также устанавливает approved=1 (даже для существующих).
    """
    is_new = False
    async with _connect() as db:
        async with db.execute("SELECT 1 FROM guilds WHERE guild_id = ?", (guild_id,)) as cur:
            existing = await cur.fetchone()
        if existing:
            # Обновляем基本信息
            await db.execute(
                "UPDATE guilds SET name = ?, icon_url = COALESCE(?, icon_url), "
                "owner_id = COALESCE(?, owner_id), member_count = ? WHERE guild_id = ?",
                (name, icon_url, owner_id, member_count, guild_id),
            )
            # Если auto_approve — переапруваем (например при re-add бота на сервер)
            if auto_approve:
                await db.execute(
                    "UPDATE guilds SET approved = 1, approved_at = ?, approved_by = 0 WHERE guild_id = ?",
                    (datetime.utcnow().isoformat(), guild_id),
                )
                log.info("Re-approved guild %s (auto_approve=True)", guild_id)
        else:
            await db.execute(
                "INSERT INTO guilds (guild_id, name, icon_url, owner_id, member_count, approved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, name, icon_url, owner_id, member_count,
                 1 if auto_approve else 0, datetime.utcnow().isoformat()),
            )
            is_new = True
        await db.commit()
    return is_new


async def approve_guild(guild_id: int, approved_by: int) -> bool:
    """Одобрить сервер. Только админ."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE guilds SET approved = 1, approved_at = ?, approved_by = ? WHERE guild_id = ?",
            (datetime.utcnow().isoformat(), approved_by, guild_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def reject_guild(guild_id: int) -> bool:
    """Отклонить (отозвать апрув). Только админ."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE guilds SET approved = 0, approved_at = NULL WHERE guild_id = ?",
            (guild_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_guild(guild_id: int) -> dict | None:
    """Получить инфу о сервере."""
    async with _connect() as db:
        async with db.execute(
            "SELECT guild_id, name, icon_url, owner_id, member_count, approved, created_at, approved_at, approved_by "
            "FROM guilds WHERE guild_id = ?",
            (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "guild_id": row[0], "name": row[1], "icon_url": row[2], "owner_id": row[3],
                "member_count": row[4], "approved": bool(row[5]), "created_at": row[6],
                "approved_at": row[7], "approved_by": row[8],
            }


async def list_guilds(approved_only: bool = False) -> list[dict]:
    """Список всех серверов."""
    async with _connect() as db:
        if approved_only:
            sql = "SELECT guild_id, name, icon_url, owner_id, member_count, approved, created_at, approved_at FROM guilds WHERE approved = 1 ORDER BY name"
        else:
            sql = "SELECT guild_id, name, icon_url, owner_id, member_count, approved, created_at, approved_at FROM guilds ORDER BY approved DESC, name"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [
            {
                "guild_id": r[0], "name": r[1], "icon_url": r[2], "owner_id": r[3],
                "member_count": r[4], "approved": bool(r[5]), "created_at": r[6],
                "approved_at": r[7],
            }
            for r in rows
        ]


async def is_guild_approved(guild_id: int) -> bool:
    """Проверить, одобрен ли сервер."""
    async with _connect() as db:
        async with db.execute(
            "SELECT approved FROM guilds WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


# === Миграция v1.0.0: старые данные → guild_0_* (default) ===

async def migrate_legacy_data_to_default_guild() -> int:
    """Мигрировать существующие данные из глобальных таблиц (watched, quotes, winners,
    wheel_items, ratings, movie_nights, settings) в guild_0_* таблицы.

    Это происходит один раз при первом запуске v1.0.0.
    Возвращает количество мигрированных строк.
    """
    # Создаём default guild таблицы (guild_id=0)
    await init_guild_tables(0)

    total_migrated = 0
    async with _connect() as db:
        # Проверяем есть ли уже данные в guild_0_* (идемпотентность)
        async with db.execute(f"SELECT COUNT(*) FROM {guild_table(0, 'watched')}") as cur:
            existing = (await cur.fetchone())[0]
        if existing > 0:
            log.info("Legacy migration already done (guild_0 has %d watched records), skipping", existing)
            return 0

        # Копируем данные из старых таблиц в guild_0_*
        migrations = [
            ("watched", "id, title, watched_at, rating, watcher_user_id"),
            ("quotes", "id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link"),
            ("winners", "id, lot_id, lot_name, tmdb_id, confidence, detected_at, confirmed_at, confirmed_by"),
            ("wheel_items", "id, name, tmdb_id, color, added_by, added_at, is_active"),
            ("ratings", "id, winner_id, user_discord_id, rating, created_at, updated_at"),
            ("movie_nights", "id, title, scheduled_at, created_by, created_at, event_id"),
        ]
        for base, cols in migrations:
            src = base  # старое имя таблицы
            dst = guild_table(0, base)
            try:
                # Копируем только если исходная таблица существует
                async with db.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (src,)) as cur:
                    if not await cur.fetchone():
                        continue
                cur = await db.execute(f"INSERT INTO {dst} ({cols}) SELECT {cols} FROM {src}")
                total_migrated += cur.rowcount
                log.info("Migrated %d rows from %s → %s", cur.rowcount, src, dst)
            except Exception as e:
                log.warning("Migration %s → %s failed: %s (table may not exist)", src, dst, e)

        # Копируем настройки — но только guild-специфичные (channel_ids, role_id, telegram_chat_id)
        # Глобальные токены остаются в settings
        guild_setting_keys = {
            "channel_quotes_id", "channel_announce_id", "channel_winners_id",
            "role_movie_ping_id", "telegram_chat_id", "tg_winners_chat_id",
            "tg_crosspost_quotes", "tg_crosspost_announce",
        }
        dst_settings = guild_table(0, "settings")
        try:
            async with db.execute("SELECT key, value, is_secret FROM settings") as cur:
                rows = await cur.fetchall()
            for key, value, is_secret in rows:
                if key in guild_setting_keys:
                    await db.execute(
                        f"INSERT OR IGNORE INTO {dst_settings} (key, value, is_secret) VALUES (?, ?, ?)",
                        (key, value, is_secret),
                    )
                    total_migrated += 1
        except Exception as e:
            log.warning("Settings migration failed: %s", e)

        await db.commit()

    if total_migrated > 0:
        log.info("Legacy migration complete: %d rows migrated to guild_0_*", total_migrated)
    return total_migrated


# === Помощники для db.py — guild-scoped аналоги существующих функций ===

async def get_guild_setting(guild_id: int, key: str) -> str | None:
    """Получить guild-специфичную настройку."""
    table = guild_table(guild_id, "settings")
    async with _connect() as db:
        async with db.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_guild_setting(guild_id: int, key: str, value: str | None, is_secret: bool = False) -> None:
    """Установить guild-специфичную настройку."""
    table = guild_table(guild_id, "settings")
    async with _connect() as db:
        await db.execute(
            f"INSERT INTO {table} (key, value, is_secret) VALUES (?, ?, ?) "
            f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret",
            (key, value, 1 if is_secret else 0),
        )
        await db.commit()


async def delete_guild_setting(guild_id: int, key: str) -> None:
    """Удалить guild-специфичную настройку."""
    table = guild_table(guild_id, "settings")
    async with _connect() as db:
        await db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
        await db.commit()

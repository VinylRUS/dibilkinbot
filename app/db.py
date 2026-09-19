"""Асинхронный доступ к SQLite. Схема создаётся при первом запуске."""
from __future__ import annotations

import aiosqlite
from datetime import datetime

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS watched (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    watched_at TEXT NOT NULL,        -- ISO8601
    rating INTEGER,                  -- 1-10 или NULL
    watcher_user_id INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watched_title ON watched(lower(title));

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    author_user_id INTEGER,
    text TEXT NOT NULL,
    recorded_by INTEGER NOT NULL,
    recorded_at TEXT NOT NULL        -- ISO8601
);

CREATE TABLE IF NOT EXISTS movie_nights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    scheduled_at TEXT NOT NULL,     -- ISO8601
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,        -- ISO8601
    event_id INTEGER                 -- Discord Scheduled Event ID
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,                       -- для секретов — зашифрован
    is_secret INTEGER DEFAULT 0
);
"""


async def init_db() -> None:
    """Создать таблицы если их нет. Вызывается при старте приложения."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _connect():
    return aiosqlite.connect(settings.database_path)


# === Watched ===

async def add_watched(title: str, rating: int | None, watcher_user_id: int) -> bool:
    """Возвращает True если добавлено, False если уже было просмотрено."""
    async with _connect() as db:
        try:
            await db.execute(
                "INSERT INTO watched (title, watched_at, rating, watcher_user_id) VALUES (?, ?, ?, ?)",
                (title, datetime.utcnow().isoformat(), rating, watcher_user_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def is_watched(title: str) -> bool:
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM watched WHERE lower(title) = lower(?) LIMIT 1", (title,)
        ) as cur:
            return await cur.fetchone() is not None


async def list_watched(limit: int = 50) -> list[tuple]:
    async with _connect() as db:
        async with db.execute(
            "SELECT title, watched_at, rating FROM watched ORDER BY watched_at DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


# === Quotes ===

async def add_quote(author: str, author_user_id: int | None, text: str, recorded_by: int) -> int:
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO quotes (author, author_user_id, text, recorded_by, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (author, author_user_id, text, recorded_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def random_quote() -> tuple | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT author, author_user_id, text, recorded_by, recorded_at "
            "FROM quotes ORDER BY RANDOM() LIMIT 1"
        ) as cur:
            return await cur.fetchone()


async def search_quotes(keyword: str, limit: int = 10) -> list[tuple]:
    async with _connect() as db:
        async with db.execute(
            "SELECT author, author_user_id, text, recorded_by, recorded_at "
            "FROM quotes WHERE text LIKE ? OR author LIKE ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", limit),
        ) as cur:
            return await cur.fetchall()


async def top_quotes(limit: int = 10) -> list[tuple]:
    """Топ цитат — по частоте автора (группировка)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT author, COUNT(*) as cnt FROM quotes GROUP BY author ORDER BY cnt DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()


# === Movie Nights ===

async def add_movie_night(title: str | None, scheduled_at: datetime, created_by: int, event_id: int | None = None) -> int:
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO movie_nights (title, scheduled_at, created_by, created_at, event_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, scheduled_at.isoformat(), created_by, datetime.utcnow().isoformat(), event_id),
        )
        await db.commit()
        return cur.lastrowid


# === Settings (с кешированием) ===

import time
_settings_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 30.0  # секунд


async def get_setting(key: str) -> str | None:
    now = time.time()
    cached = _settings_cache.get(key)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]
    async with _connect() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            val = row[0] if row else None
            _settings_cache[key] = (val, now)
            return val


async def set_setting(key: str, value: str | None, is_secret: bool = False) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO settings (key, value, is_secret) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret",
            (key, value, 1 if is_secret else 0),
        )
        await db.commit()
    _settings_cache.pop(key, None)


async def list_settings() -> list[tuple]:
    async with _connect() as db:
        async with db.execute("SELECT key, value, is_secret FROM settings ORDER BY key") as cur:
            return await cur.fetchall()


async def delete_setting(key: str) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()
    _settings_cache.pop(key, None)

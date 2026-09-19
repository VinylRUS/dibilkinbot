"""Асинхронный доступ к SQLite. Схема v2: users, tg_links, movie_meta, winners + старые watched/quotes/movie_nights/settings."""
from __future__ import annotations

import aiosqlite
import logging
import time
from datetime import datetime
from pathlib import Path

from config import settings

log = logging.getLogger("db")

SCHEMA = """
-- Старое: watched
CREATE TABLE IF NOT EXISTS watched (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    watched_at TEXT NOT NULL,
    rating INTEGER,
    watcher_user_id INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watched_title ON watched(lower(title));

-- Старое: quotes
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    author_user_id INTEGER,
    text TEXT NOT NULL,
    recorded_by INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

-- Старое: movie_nights
CREATE TABLE IF NOT EXISTS movie_nights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    scheduled_at TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    event_id INTEGER
);

-- Старое: settings (с кешем)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    is_secret INTEGER DEFAULT 0
);

-- === НОВОЕ v2 ===

-- Пользователи панели (Discord ID)
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

-- Связь Discord ↔ Telegram (подтверждается кодом)
CREATE TABLE IF NOT EXISTS tg_links (
    discord_id INTEGER PRIMARY KEY,
    tg_user_id INTEGER,
    tg_username TEXT,
    link_code TEXT,                  -- 6-значный код верификации
    link_code_expires_at TEXT,        -- ISO8601, когда код протухнет
    linked_at TEXT
);

-- Кеш метаданных фильмов из TMDB (TTL 7 дней)
CREATE TABLE IF NOT EXISTS movie_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_title TEXT NOT NULL,       -- что искал юзер (lowercase)
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,              -- локализованный (RU)
    original_title TEXT,
    year TEXT,
    release_date TEXT,
    poster_url TEXT,
    plot TEXT,
    vote_average REAL,
    vote_count INTEGER,
    genres TEXT,                     -- JSON array as string
    runtime INTEGER,
    tagline TEXT,
    imdb_id TEXT,
    cached_at TEXT NOT NULL           -- ISO8601
);
CREATE INDEX IF NOT EXISTS idx_movie_meta_query ON movie_meta(lower(query_title));
CREATE UNIQUE INDEX IF NOT EXISTS idx_movie_meta_tmdb ON movie_meta(tmdb_id);

-- Победители колеса (лог всех когда-либо выпавших)
CREATE TABLE IF NOT EXISTS winners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT,                     -- ID лота из Pointauc (если известен)
    lot_name TEXT NOT NULL,          -- название фильма как в колесе
    tmdb_id INTEGER,                 -- связка с movie_meta, если найдено
    confidence TEXT NOT NULL,        -- 'unconfirmed' | 'confirmed'
    detected_at TEXT NOT NULL,       -- ISO8601
    confirmed_at TEXT,               -- когда юзер подтвердил через /watched
    confirmed_by INTEGER             -- Discord ID подтвердившего
);
CREATE INDEX IF NOT EXISTS idx_winners_detected ON winners(detected_at DESC);

-- === НОВОЕ v3: собственное колесо (без Pointauc) ===
CREATE TABLE IF NOT EXISTS wheel_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,              -- название фильма (как в колесе)
    tmdb_id INTEGER,                  -- связка с movie_meta (если найдено через Кинопоиск)
    color TEXT,                       -- HEX цвет сектора на колесе (авто-генерация если NULL)
    added_by INTEGER NOT NULL,        -- Discord ID добавившего
    added_at TEXT NOT NULL,           -- ISO8601
    is_active INTEGER DEFAULT 1      -- 1 = в колесе, 0 = удалён (soft delete для истории)
);
CREATE INDEX IF NOT EXISTS idx_wheel_active ON wheel_items(is_active);
"""


async def init_db() -> None:
    """Создать таблицы если их нет."""
    try:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("DB dir: %s", settings.database_path.parent)
    except (OSError, PermissionError) as e:
        log.error("CANNOT CREATE DB DIR %s: %s", settings.database_path.parent, e)
        raise

    try:
        async with aiosqlite.connect(settings.database_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        log.info("DB initialized at %s (size=%d bytes)",
                 settings.database_path, settings.database_path.stat().st_size)
    except Exception as e:
        log.error("DB INIT FAILED at %s: %s", settings.database_path, e)
        raise


def _connect():
    return aiosqlite.connect(settings.database_path)


# === Settings (с кешированием) ===
_settings_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 30.0


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


# === Watched ===

async def add_watched(title: str, rating: int | None, watcher_user_id: int) -> bool:
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


# === Users ===

async def upsert_user(discord_id: int, username: str | None = None, display_name: str | None = None) -> None:
    """Создать или обновить запись пользователя. Не трогает is_admin."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO users (discord_id, username, display_name, created_at, last_login_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET "
            "  username = excluded.username, "
            "  display_name = excluded.display_name, "
            "  last_login_at = excluded.last_login_at",
            (discord_id, username, display_name,
             datetime.utcnow().isoformat(), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_user(discord_id: int) -> tuple | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, is_admin FROM users WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            return await cur.fetchone()


async def is_admin(discord_id: int) -> bool:
    """Админ — если:
    1. discord_id совпадает с env ADMIN_DISCORD_ID (высший приоритет), ИЛИ
    2. discord_id совпадает с env ADMIN_LOGIN (если ADMIN_LOGIN — это число), ИЛИ
    3. is_admin=1 в БД для этого юзера.
    """
    # 1. env ADMIN_DISCORD_ID (новая, приоритетная)
    if settings.admin_discord_id is not None and settings.admin_discord_id == discord_id:
        return True
    # 2. env ADMIN_LOGIN если это число (legacy-поддержка)
    env_admin = settings.admin_login
    if env_admin.isdigit() and int(env_admin) == discord_id:
        return True
    # 3. is_admin флаг в БД
    user = await get_user(discord_id)
    return bool(user and user[3] == 1)


async def list_users() -> list[tuple]:
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, is_admin, last_login_at "
            "FROM users ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()


async def set_admin(discord_id: int, is_admin_flag: bool) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET is_admin = ? WHERE discord_id = ?",
            (1 if is_admin_flag else 0, discord_id),
        )
        await db.commit()


# === TG Links ===

async def create_tg_link_code(discord_id: int, code: str, expires_at: datetime) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO tg_links (discord_id, link_code, link_code_expires_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET "
            "  link_code = excluded.link_code, "
            "  link_code_expires_at = excluded.link_code_expires_at, "
            "  tg_user_id = NULL, "
            "  tg_username = NULL, "
            "  linked_at = NULL",
            (discord_id, code, expires_at.isoformat()),
        )
        await db.commit()


async def get_tg_link(discord_id: int) -> tuple | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, tg_user_id, tg_username, link_code, link_code_expires_at, linked_at "
            "FROM tg_links WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            return await cur.fetchone()


async def verify_tg_link(code: str, tg_user_id: int, tg_username: str | None) -> int | None:
    """Возвращает discord_id если код валиден и не протух, иначе None."""
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id FROM tg_links "
            "WHERE link_code = ? AND link_code_expires_at > ?",
            (code, now)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            discord_id = row[0]
            await db.execute(
                "UPDATE tg_links SET tg_user_id = ?, tg_username = ?, linked_at = ?, "
                "link_code = NULL, link_code_expires_at = NULL WHERE discord_id = ?",
                (tg_user_id, tg_username, now, discord_id),
            )
            await db.commit()
            return discord_id


# === Movie Meta (TMDB cache) ===

MOVIE_META_TTL_DAYS = 7


async def get_movie_meta(query_title: str) -> tuple | None:
    """Возвращает кешированную мета-информацию если она не протухла."""
    cutoff = (datetime.utcnow().timestamp() - MOVIE_META_TTL_DAYS * 86400)
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT tmdb_id, title, original_title, year, release_date, "
            "       poster_url, plot, vote_average, vote_count, genres, runtime, tagline, imdb_id, cached_at "
            "FROM movie_meta WHERE lower(query_title) = lower(?) AND cached_at > ?",
            (query_title, cutoff_iso)
        ) as cur:
            return await cur.fetchone()


async def save_movie_meta(query_title: str, meta: dict) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO movie_meta (query_title, tmdb_id, title, original_title, year, "
            "  release_date, poster_url, plot, vote_average, vote_count, genres, runtime, tagline, imdb_id, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tmdb_id) DO UPDATE SET "
            "  query_title = excluded.query_title, "
            "  title = excluded.title, "
            "  poster_url = excluded.poster_url, "
            "  plot = excluded.plot, "
            "  vote_average = excluded.vote_average, "
            "  vote_count = excluded.vote_count, "
            "  cached_at = excluded.cached_at",
            (query_title, meta["tmdb_id"], meta["title"], meta.get("original_title"),
             meta.get("year"), meta.get("release_date"), meta.get("poster_url"),
             meta.get("plot", ""), meta.get("vote_average", 0), meta.get("vote_count", 0),
             meta.get("genres_json", "[]"), meta.get("runtime"), meta.get("tagline"),
             meta.get("imdb_id"), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_movie_meta_by_tmdb_id(tmdb_id: int) -> tuple | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT title, year, poster_url, plot, vote_average, vote_count, genres, runtime, tagline, imdb_id "
            "FROM movie_meta WHERE tmdb_id = ?",
            (tmdb_id,)
        ) as cur:
            return await cur.fetchone()


# === Winners ===

async def add_winner(lot_id: str | None, lot_name: str, tmdb_id: int | None,
                    confidence: str = "unconfirmed") -> int:
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO winners (lot_id, lot_name, tmdb_id, confidence, detected_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lot_id, lot_name, tmdb_id, confidence, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def confirm_winner(winner_id: int, confirmed_by: int) -> bool:
    """Подтвердить победителя через /watched. Возвращает True если подтверждено."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE winners SET confidence = 'confirmed', confirmed_at = ?, confirmed_by = ? "
            "WHERE id = ? AND confidence = 'unconfirmed'",
            (datetime.utcnow().isoformat(), confirmed_by, winner_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_winners(limit: int = 20) -> list[tuple]:
    async with _connect() as db:
        async with db.execute(
            "SELECT id, lot_name, tmdb_id, confidence, detected_at, confirmed_at "
            "FROM winners ORDER BY detected_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            return await cur.fetchall()


async def get_recent_unconfirmed_winners(minutes: int = 5) -> list[tuple]:
    """Возвращает unconfirmed победителей за последние N минут (для авто-связки с /watched)."""
    cutoff = (datetime.utcnow().timestamp() - minutes * 60)
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT id, lot_name, tmdb_id FROM winners "
            "WHERE confidence = 'unconfirmed' AND detected_at > ?",
            (cutoff_iso,)
        ) as cur:
            return await cur.fetchall()


# === Утилита для веб-панели: последние события ===

async def recent_activity(limit: int = 20) -> list[dict]:
    """Сводная лента: последние победители + последние просмотренные, отсортированные по времени."""
    items: list[dict] = []
    async with _connect() as db:
        async with db.execute(
            "SELECT 'winner' AS type, id, lot_name AS name, detected_at AS ts, confidence "
            "FROM winners ORDER BY detected_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            for row in await cur.fetchall():
                items.append({
                    "type": "winner", "id": row[1], "name": row[2],
                    "ts": row[3], "confidence": row[4]
                })
        async with db.execute(
            "SELECT 'watched' AS type, id, title, watched_at, rating "
            "FROM watched ORDER BY watched_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            for row in await cur.fetchall():
                items.append({
                    "type": "watched", "id": row[1], "name": row[2],
                    "ts": row[3], "rating": row[4]
                })
    # Сортируем по ts DESC, берём top N
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# === Wheel Items (собственное колесо, без Pointauc) ===

# Палитра цветов секторов — тёплая, "медовая"
WHEEL_COLORS = [
    "#FFB703", "#FB8500", "#FF6B35", "#F4A261",
    "#E76F51", "#F9C74F", "#90BE6D", "#43AA8B",
    "#577590", "#277DA1", "#6A4C93", "#1982C4",
]


def _pick_color(index: int) -> str:
    """Циклически выбрать цвет из палитры."""
    return WHEEL_COLORS[index % len(WHEEL_COLORS)]


async def add_wheel_item(name: str, tmdb_id: int | None, added_by: int) -> int:
    """Добавить лот в колесо. Возвращает id нового лота."""
    async with _connect() as db:
        # Считаем сколько уже активных лотов — для выбора цвета
        async with db.execute("SELECT COUNT(*) FROM wheel_items WHERE is_active = 1") as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
        color = _pick_color(count)
        cur = await db.execute(
            "INSERT INTO wheel_items (name, tmdb_id, color, added_by, added_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (name, tmdb_id, color, added_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def list_wheel_items(active_only: bool = True) -> list[dict]:
    """Список лотов колеса. Возвращает list of dicts: id, name, tmdb_id, color, added_by, added_at."""
    async with _connect() as db:
        if active_only:
            sql = "SELECT id, name, tmdb_id, color, added_by, added_at FROM wheel_items WHERE is_active = 1 ORDER BY id"
        else:
            sql = "SELECT id, name, tmdb_id, color, added_by, added_at FROM wheel_items ORDER BY id DESC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "tmdb_id": r[2], "color": r[3],
             "added_by": r[4], "added_at": r[5]}
            for r in rows
        ]


async def remove_wheel_item(item_id: int) -> bool:
    """Soft-delete лота (is_active = 0). Возвращает True если удалён, False если не найден."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE wheel_items SET is_active = 0 WHERE id = ? AND is_active = 1",
            (item_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def clear_wheel() -> int:
    """Удалить все лоты (soft-delete). Возвращает сколько удалил."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE wheel_items SET is_active = 0 WHERE is_active = 1"
        )
        await db.commit()
        return cur.rowcount


async def get_wheel_item(item_id: int) -> dict | None:
    """Получить конкретный лот по id."""
    async with _connect() as db:
        async with db.execute(
            "SELECT id, name, tmdb_id, color FROM wheel_items WHERE id = ? AND is_active = 1",
            (item_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "name": row[1], "tmdb_id": row[2], "color": row[3]}


async def reassign_colors() -> None:
    """Пересчитать цвета для всех активных лотов (после удаления чтобы порядок был красивый)."""
    items = await list_wheel_items(active_only=True)
    async with _connect() as db:
        for i, item in enumerate(items):
            await db.execute(
                "UPDATE wheel_items SET color = ? WHERE id = ?",
                (_pick_color(i), item["id"]),
            )
        await db.commit()


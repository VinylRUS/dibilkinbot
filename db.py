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

-- Цитатник
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    author_user_id INTEGER,
    author_avatar_url TEXT,           -- v0.9.0: аватар автора для embed
    text TEXT NOT NULL,
    recorded_by INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    message_link TEXT                  -- v0.9.0: ссылка на исходное сообщение (если есть)
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
    avatar_url TEXT,
    roles TEXT,                       -- JSON array of role names
    top_role TEXT,
    guild_name TEXT,                  -- имя сервера где бот его нашёл
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
    lot_id TEXT,                     -- ID лота из колеса
    lot_name TEXT NOT NULL,          -- название фильма как в колесе
    tmdb_id INTEGER,                 -- связка с movie_meta, если найдено
    confidence TEXT NOT NULL,        -- 'unconfirmed' | 'confirmed'
    detected_at TEXT NOT NULL,       -- ISO8601
    confirmed_at TEXT,               -- когда юзер подтвердил через /watched
    confirmed_by INTEGER             -- Discord ID подтвердившего
);
CREATE INDEX IF NOT EXISTS idx_winners_detected ON winners(detected_at DESC);

-- === НОВОЕ v3: собственное колесо ===
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

-- === НОВОЕ v0.8.0: ratings (community rating) ===
-- Каждый юзер может поставить одну оценку (1-10) победителю колеса.
-- UNIQUE(winner_id, user_discord_id) — один юзер = одна оценка на фильм (можно переголосовать).
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    winner_id INTEGER NOT NULL,         -- FK → winners.id
    user_discord_id INTEGER NOT NULL,    -- кто поставил
    rating INTEGER NOT NULL,            -- 1-10
    created_at TEXT NOT NULL,           -- ISO8601
    updated_at TEXT,                    -- когда переголосовал
    FOREIGN KEY (winner_id) REFERENCES winners(id),
    UNIQUE(winner_id, user_discord_id)
);
CREATE INDEX IF NOT EXISTS idx_ratings_winner ON ratings(winner_id);
CREATE INDEX IF NOT EXISTS idx_ratings_user ON ratings(user_discord_id);
"""


async def init_db() -> None:
    """Создать таблицы если их нет. Плюс миграции (добавление новых колонок в существующие таблицы)."""
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

            # === Миграции: добавляем колонки если их ещё нет ===
            # users: avatar_url, roles, top_role, guild_name (появились в v0.6.0)
            async with db.execute("PRAGMA table_info(users)") as cur:
                existing_cols = {row[1] for row in await cur.fetchall()}
            new_cols = {
                "avatar_url": "TEXT",
                "roles": "TEXT",
                "top_role": "TEXT",
                "guild_name": "TEXT",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    log.info("Migrating users: adding column %s", col_name)
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            await db.commit()

            # Миграция v0.9.0: quotes — добавляем author_avatar_url и message_link
            async with db.execute("PRAGMA table_info(quotes)") as cur:
                existing_quote_cols = {row[1] for row in await cur.fetchall()}
            new_quote_cols = {
                "author_avatar_url": "TEXT",
                "message_link": "TEXT",
            }
            for col_name, col_type in new_quote_cols.items():
                if col_name not in existing_quote_cols:
                    log.info("Migrating quotes: adding column %s", col_name)
                    await db.execute(f"ALTER TABLE quotes ADD COLUMN {col_name} {col_type}")
            await db.commit()

            # === Миграция v0.8.0: старые /watched записи → ratings + winners ===
            # Для каждой записи в watched (без рейтинга или с rating):
            #   1. Создаём winner (если его ещё нет) с тем же title
            #   2. Создаём rating от watcher_user_id (или 0 если не было рейтинга)
            async with db.execute("SELECT id, title, watched_at, rating, watcher_user_id FROM watched") as cur:
                watched_rows = await cur.fetchall()
            migrated_count = 0
            for w_id, title, watched_at, rating_val, watcher_id in watched_rows:
                # Ищем существующего winner с таким же title
                async with db.execute(
                    "SELECT id FROM winners WHERE lower(lot_name) = lower(?) LIMIT 1",
                    (title,)
                ) as w_cur:
                    w_row = await w_cur.fetchone()
                if w_row:
                    winner_id = w_row[0]
                else:
                    # Создаём winner с confidence='confirmed'
                    cur_w = await db.execute(
                        "INSERT INTO winners (lot_name, tmdb_id, confidence, detected_at, confirmed_at, confirmed_by) "
                        "VALUES (?, NULL, 'confirmed', ?, ?, ?)",
                        (title, watched_at, watched_at, watcher_id or 0),
                    )
                    winner_id = cur_w.lastrowid

                # Если был rating — создаём rating запись (если ещё нет)
                if rating_val and 1 <= rating_val <= 10:
                    try:
                        await db.execute(
                            "INSERT INTO ratings (winner_id, user_discord_id, rating, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (winner_id, watcher_id or 0, rating_val, watched_at),
                        )
                        migrated_count += 1
                    except aiosqlite.IntegrityError:
                        pass  # уже есть — пропускаем
            if migrated_count > 0:
                log.info("Migrated %d old watched records to ratings", migrated_count)
            await db.commit()

            # === Миграция v1.0.0: multi-tenant ===
            # Создаём таблицу guilds + переносим старые данные в guild_0_* (default)
            try:
                import guild as guild_module
                await guild_module.init_guild_registry()
                migrated = await guild_module.migrate_legacy_data_to_default_guild()
                if migrated > 0:
                    log.info("v1.0.0 migration: %d rows moved to guild_0_* tables", migrated)
            except Exception as e:
                log.error("v1.0.0 guild migration failed: %s", e)

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
            "SELECT id, title, watched_at, rating FROM watched ORDER BY watched_at DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


async def delete_watched(watched_id: int) -> bool:
    """Удалить запись из watched по id. Возвращает True если удалено."""
    async with _connect() as db:
        cur = await db.execute("DELETE FROM watched WHERE id = ?", (watched_id,))
        await db.commit()
        return cur.rowcount > 0


# === Quotes ===

async def add_quote(
    author: str,
    author_user_id: int | None,
    text: str,
    recorded_by: int,
    author_avatar_url: str | None = None,
    message_link: str | None = None,
) -> int:
    """Добавить цитату. Возвращает id новой цитаты."""
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO quotes (author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (author, author_user_id, author_avatar_url, text, recorded_by,
             datetime.utcnow().isoformat(), message_link),
        )
        await db.commit()
        return cur.lastrowid


async def get_quote(quote_id: int) -> tuple | None:
    """Получить цитату по id. Возвращает tuple с полями:
    (id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link)
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
            "FROM quotes WHERE id = ?",
            (quote_id,)
        ) as cur:
            return await cur.fetchone()


async def list_quotes(limit: int = 50, offset: int = 0, search: str | None = None) -> list[tuple]:
    """Список цитат с пагинацией и опциональным поиском.
    Поиск ищет в author и text (case-insensitive LIKE).
    """
    async with _connect() as db:
        if search:
            sql = (
                "SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
                "FROM quotes WHERE text LIKE ? OR author LIKE ? "
                "ORDER BY recorded_at DESC LIMIT ? OFFSET ?"
            )
            params = (f"%{search}%", f"%{search}%", limit, offset)
        else:
            sql = (
                "SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
                "FROM quotes ORDER BY recorded_at DESC LIMIT ? OFFSET ?"
            )
            params = (limit, offset)
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


async def count_quotes(search: str | None = None) -> int:
    """Подсчитать количество цитат (с опциональным поиском)."""
    async with _connect() as db:
        if search:
            async with db.execute(
                "SELECT COUNT(*) FROM quotes WHERE text LIKE ? OR author LIKE ?",
                (f"%{search}%", f"%{search}%")
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute("SELECT COUNT(*) FROM quotes") as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


async def delete_quote(quote_id: int) -> bool:
    """Удалить цитату по id. Возвращает True если удалено."""
    async with _connect() as db:
        cur = await db.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
        await db.commit()
        return cur.rowcount > 0


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

async def upsert_user(
    discord_id: int,
    username: str | None = None,
    display_name: str | None = None,
    avatar_url: str | None = None,
    roles: list[str] | None = None,
    top_role: str | None = None,
    guild_name: str | None = None,
) -> None:
    """Создать или обновить запись пользователя. Не трогает is_admin."""
    import json
    roles_json = json.dumps(roles, ensure_ascii=False) if roles else None
    async with _connect() as db:
        await db.execute(
            "INSERT INTO users (discord_id, username, display_name, avatar_url, roles, top_role, guild_name, created_at, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET "
            "  username = excluded.username, "
            "  display_name = excluded.display_name, "
            "  avatar_url = COALESCE(excluded.avatar_url, users.avatar_url), "
            "  roles = COALESCE(excluded.roles, users.roles), "
            "  top_role = COALESCE(excluded.top_role, users.top_role), "
            "  guild_name = COALESCE(excluded.guild_name, users.guild_name), "
            "  last_login_at = excluded.last_login_at",
            (discord_id, username, display_name, avatar_url, roles_json, top_role, guild_name,
             datetime.utcnow().isoformat(), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_user(discord_id: int) -> tuple | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, is_admin, avatar_url, roles, top_role, guild_name, last_login_at "
            "FROM users WHERE discord_id = ?",
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
            "SELECT discord_id, username, display_name, is_admin, last_login_at, avatar_url, top_role, guild_name "
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


async def delete_winner(winner_id: int) -> bool:
    """Удалить запись из winners по id. Возвращает True если удалено.
    Также каскадно удаляет все оценки (ratings) этого победителя.
    """
    async with _connect() as db:
        # Сначала удаляем оценки
        await db.execute("DELETE FROM ratings WHERE winner_id = ?", (winner_id,))
        cur = await db.execute("DELETE FROM winners WHERE id = ?", (winner_id,))
        await db.commit()
        return cur.rowcount > 0


# === Ratings (community rating, v0.8.0) ===

async def upsert_rating(winner_id: int, user_discord_id: int, rating: int) -> bool:
    """Поставить или обновить оценку победителю.
    Один юзер = одна оценка на фильм (UNIQUE constraint).
    Возвращает True если оценка поставлена, False если рейтинг вне диапазона 1-10.

    Побочный эффект: при первой оценке победитель "переезжает" в watched
    (если ещё не там) — INSERT INTO watched, чтобы он появился в бэклоге.
    """
    if not (1 <= rating <= 10):
        return False

    async with _connect() as db:
        # INSERT OR UPDATE (если уже есть оценка — обновляем)
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO ratings (winner_id, user_discord_id, rating, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(winner_id, user_discord_id) DO UPDATE SET "
            "  rating = excluded.rating, "
            "  updated_at = excluded.updated_at",
            (winner_id, user_discord_id, rating, now, now),
        )
        await db.commit()

        # Проверяем, есть ли уже запись в watched (по lot_name победителя)
        async with db.execute("SELECT lot_name FROM winners WHERE id = ?", (winner_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            lot_name = row[0]

        # Проверяем, есть ли уже в watched (case-insensitive)
        async with db.execute(
            "SELECT 1 FROM watched WHERE lower(title) = lower(?) LIMIT 1",
            (lot_name,)
        ) as cur:
            if not await cur.fetchone():
                # Не в watched — добавляем (первая оценка = переезд в бэклог)
                try:
                    await db.execute(
                        "INSERT INTO watched (title, watched_at, rating, watcher_user_id) VALUES (?, ?, ?, ?)",
                        (lot_name, now, rating, user_discord_id),
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    pass  # race condition — уже добавлено кем-то другим

        return True


async def get_ratings_for_winner(winner_id: int) -> list[tuple]:
    """Все оценки победителя: [(user_discord_id, rating, created_at, updated_at), ...]."""
    async with _connect() as db:
        async with db.execute(
            "SELECT user_discord_id, rating, created_at, updated_at "
            "FROM ratings WHERE winner_id = ? ORDER BY created_at",
            (winner_id,)
        ) as cur:
            return await cur.fetchall()


async def get_average_rating(winner_id: int) -> tuple[float, int]:
    """Средний рейтинг победителя и количество оценок. (0.0, 0) если нет оценок."""
    async with _connect() as db:
        async with db.execute(
            "SELECT AVG(rating), COUNT(*) FROM ratings WHERE winner_id = ?",
            (winner_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row or not row[1]:
                return 0.0, 0
            return round(row[0], 1), row[1]


async def get_user_rating(winner_id: int, user_discord_id: int) -> int | None:
    """Оценка конкретного юзера для победителя (или None)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT rating FROM ratings WHERE winner_id = ? AND user_discord_id = ?",
            (winner_id, user_discord_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_winners_with_ratings(limit: int = 50) -> list[dict]:
    """Победители с агрегированной информацией о рейтингах.
    Возвращает list of dicts с полями:
      id, lot_name, tmdb_id, confidence, detected_at, confirmed_at,
      avg_rating, ratings_count, user_rating (текущего юзера если задан)
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT w.id, w.lot_name, w.tmdb_id, w.confidence, w.detected_at, w.confirmed_at, "
            "  COALESCE(AVG(r.rating), 0) as avg_rating, "
            "  COUNT(r.id) as ratings_count "
            "FROM winners w "
            "LEFT JOIN ratings r ON r.winner_id = w.id "
            "GROUP BY w.id "
            "ORDER BY w.detected_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "lot_name": r[1], "tmdb_id": r[2], "confidence": r[3],
                "detected_at": r[4], "confirmed_at": r[5],
                "avg_rating": round(r[6], 1) if r[6] else 0.0,
                "ratings_count": r[7],
            }
            for r in rows
        ]


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
    """Сводная лента: последние победители (с рейтингами) + последние просмотренные,
    отсортированные по времени. Возвращает list of dicts.
    """
    items: list[dict] = []
    async with _connect() as db:
        # Победители с агрегированными рейтингами
        async with db.execute(
            "SELECT w.id, w.lot_name, w.detected_at, w.confidence, "
            "  COALESCE(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as ratings_count "
            "FROM winners w "
            "LEFT JOIN ratings r ON r.winner_id = w.id "
            "GROUP BY w.id "
            "ORDER BY w.detected_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            for row in await cur.fetchall():
                items.append({
                    "type": "winner", "id": row[0], "name": row[1],
                    "ts": row[2], "confidence": row[3],
                    "avg_rating": round(row[4], 1) if row[4] else 0.0,
                    "ratings_count": row[5],
                })
        # Просмотренные (только те, что не из числа победителей — у тех уже есть выше)
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


# === Wheel Items (собственное колесо) ===

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



# === MULTI-TENANT v1.0.0: guild-scoped функции с префиксом g_ ===
# Все принимают guild_id первым параметром. Работают с guild_{id}_* таблицами.
# Старые функции (без g_) остаются как aliases к guild_id=0 (default guild).

import guild as _guild


# --- g_watched ---

async def g_add_watched(guild_id: int, title: str, rating: int | None, watcher_user_id: int) -> bool:
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        try:
            await db.execute(
                f"INSERT INTO {table} (title, watched_at, rating, watcher_user_id) VALUES (?, ?, ?, ?)",
                (title, datetime.utcnow().isoformat(), rating, watcher_user_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def g_is_watched(guild_id: int, title: str) -> bool:
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        async with db.execute(
            f"SELECT 1 FROM {table} WHERE lower(title) = lower(?) LIMIT 1", (title,)
        ) as cur:
            return await cur.fetchone() is not None


async def g_list_watched(guild_id: int, limit: int = 50) -> list[tuple]:
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, title, watched_at, rating FROM {table} ORDER BY watched_at DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


async def g_delete_watched(guild_id: int, watched_id: int) -> bool:
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        cur = await db.execute(f"DELETE FROM {table} WHERE id = ?", (watched_id,))
        await db.commit()
        return cur.rowcount > 0


# --- g_quotes ---

async def g_add_quote(guild_id: int, author: str, author_user_id: int | None, text: str,
                     recorded_by: int, author_avatar_url: str | None = None,
                     message_link: str | None = None) -> int:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        cur = await db.execute(
            f"INSERT INTO {table} (author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            (author, author_user_id, author_avatar_url, text, recorded_by,
             datetime.utcnow().isoformat(), message_link),
        )
        await db.commit()
        return cur.lastrowid


async def g_get_quote(guild_id: int, quote_id: int) -> tuple | None:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
            f"FROM {table} WHERE id = ?", (quote_id,)
        ) as cur:
            return await cur.fetchone()


async def g_list_quotes(guild_id: int, limit: int = 50, offset: int = 0, search: str | None = None) -> list[tuple]:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        if search:
            sql = (f"SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
                    f"FROM {table} WHERE text LIKE ? OR author LIKE ? ORDER BY recorded_at DESC LIMIT ? OFFSET ?")
            params = (f"%{search}%", f"%{search}%", limit, offset)
        else:
            sql = (f"SELECT id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link "
                    f"FROM {table} ORDER BY recorded_at DESC LIMIT ? OFFSET ?")
            params = (limit, offset)
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


async def g_count_quotes(guild_id: int, search: str | None = None) -> int:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        if search:
            async with db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE text LIKE ? OR author LIKE ?",
                (f"%{search}%", f"%{search}%")
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


async def g_delete_quote(guild_id: int, quote_id: int) -> bool:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        cur = await db.execute(f"DELETE FROM {table} WHERE id = ?", (quote_id,))
        await db.commit()
        return cur.rowcount > 0


async def g_top_quotes(guild_id: int, limit: int = 10) -> list[tuple]:
    table = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        async with db.execute(
            f"SELECT author, COUNT(*) as cnt FROM {table} GROUP BY author ORDER BY cnt DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


# --- g_winners ---

async def g_add_winner(guild_id: int, lot_id: str | None, lot_name: str, tmdb_id: int | None,
                      confidence: str = "unconfirmed") -> int:
    table = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        cur = await db.execute(
            f"INSERT INTO {table} (lot_id, lot_name, tmdb_id, confidence, detected_at) VALUES (?, ?, ?, ?, ?)",
            (lot_id, lot_name, tmdb_id, confidence, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def g_confirm_winner(guild_id: int, winner_id: int, confirmed_by: int) -> bool:
    table = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET confidence = 'confirmed', confirmed_at = ?, confirmed_by = ? "
            f"WHERE id = ? AND confidence = 'unconfirmed'",
            (datetime.utcnow().isoformat(), confirmed_by, winner_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_list_winners(guild_id: int, limit: int = 20) -> list[tuple]:
    table = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, lot_name, tmdb_id, confidence, detected_at, confirmed_at FROM {table} "
            f"ORDER BY detected_at DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


async def g_delete_winner(guild_id: int, winner_id: int) -> bool:
    table_w = _guild.guild_table(guild_id, "winners")
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        await db.execute(f"DELETE FROM {table_r} WHERE winner_id = ?", (winner_id,))
        cur = await db.execute(f"DELETE FROM {table_w} WHERE id = ?", (winner_id,))
        await db.commit()
        return cur.rowcount > 0


async def g_get_winners_with_ratings(guild_id: int, limit: int = 50) -> list[dict]:
    table_w = _guild.guild_table(guild_id, "winners")
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT w.id, w.lot_name, w.tmdb_id, w.confidence, w.detected_at, w.confirmed_at, "
            f"  COALESCE(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as ratings_count "
            f"FROM {table_w} w LEFT JOIN {table_r} r ON r.winner_id = w.id "
            f"GROUP BY w.id ORDER BY w.detected_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "lot_name": r[1], "tmdb_id": r[2], "confidence": r[3],
             "detected_at": r[4], "confirmed_at": r[5],
             "avg_rating": round(r[6], 1) if r[6] else 0.0, "ratings_count": r[7]}
            for r in rows
        ]


# --- g_ratings ---

async def g_upsert_rating(guild_id: int, winner_id: int, user_discord_id: int, rating: int) -> bool:
    if not (1 <= rating <= 10):
        return False
    table_r = _guild.guild_table(guild_id, "ratings")
    table_w = _guild.guild_table(guild_id, "watched")
    table_winners = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            f"INSERT INTO {table_r} (winner_id, user_discord_id, rating, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(winner_id, user_discord_id) DO UPDATE SET rating = excluded.rating, updated_at = excluded.updated_at",
            (winner_id, user_discord_id, rating, now, now),
        )
        await db.commit()
        # Если ещё не в watched — добавляем (первая оценка = переезд в бэклог)
        async with db.execute(f"SELECT lot_name FROM {table_winners} WHERE id = ?", (winner_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            lot_name = row[0]
        async with db.execute(
            f"SELECT 1 FROM {table_w} WHERE lower(title) = lower(?) LIMIT 1", (lot_name,)
        ) as cur:
            if not await cur.fetchone():
                try:
                    await db.execute(
                        f"INSERT INTO {table_w} (title, watched_at, rating, watcher_user_id) VALUES (?, ?, ?, ?)",
                        (lot_name, now, rating, user_discord_id),
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    pass
        return True


async def g_get_average_rating(guild_id: int, winner_id: int) -> tuple[float, int]:
    table = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT AVG(rating), COUNT(*) FROM {table} WHERE winner_id = ?", (winner_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row or not row[1]:
                return 0.0, 0
            return round(row[0], 1), row[1]


async def g_get_user_rating(guild_id: int, winner_id: int, user_discord_id: int) -> int | None:
    table = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT rating FROM {table} WHERE winner_id = ? AND user_discord_id = ?",
            (winner_id, user_discord_id)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# --- g_wheel_items ---

async def g_add_wheel_item(guild_id: int, name: str, tmdb_id: int | None, added_by: int) -> int:
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        async with db.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active = 1") as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
        color = _pick_color(count)
        cur = await db.execute(
            f"INSERT INTO {table} (name, tmdb_id, color, added_by, added_at, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (name, tmdb_id, color, added_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def g_list_wheel_items(guild_id: int, active_only: bool = True) -> list[dict]:
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        if active_only:
            sql = f"SELECT id, name, tmdb_id, color, added_by, added_at FROM {table} WHERE is_active = 1 ORDER BY id"
        else:
            sql = f"SELECT id, name, tmdb_id, color, added_by, added_at FROM {table} ORDER BY id DESC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [{"id": r[0], "name": r[1], "tmdb_id": r[2], "color": r[3],
                 "added_by": r[4], "added_at": r[5]} for r in rows]


async def g_remove_wheel_item(guild_id: int, item_id: int) -> bool:
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET is_active = 0 WHERE id = ? AND is_active = 1", (item_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def g_clear_wheel(guild_id: int) -> int:
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        cur = await db.execute(f"UPDATE {table} SET is_active = 0 WHERE is_active = 1")
        await db.commit()
        return cur.rowcount


async def g_reassign_colors(guild_id: int) -> None:
    items = await g_list_wheel_items(guild_id, active_only=True)
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        for i, item in enumerate(items):
            await db.execute(
                f"UPDATE {table} SET color = ? WHERE id = ?", (_pick_color(i), item["id"])
            )
        await db.commit()


# --- g_movie_nights ---

async def g_add_movie_night(guild_id: int, title: str | None, scheduled_at: datetime,
                           created_by: int, event_id: int | None = None) -> int:
    table = _guild.guild_table(guild_id, "movie_nights")
    async with _connect() as db:
        cur = await db.execute(
            f"INSERT INTO {table} (title, scheduled_at, created_by, created_at, event_id) VALUES (?, ?, ?, ?, ?)",
            (title, scheduled_at.isoformat(), created_by,
             datetime.utcnow().isoformat(), event_id),
        )
        await db.commit()
        return cur.lastrowid


# --- g_recent_activity ---

async def g_recent_activity(guild_id: int, limit: int = 20) -> list[dict]:
    """Сводная лента для конкретного guild."""
    table_w = _guild.guild_table(guild_id, "winners")
    table_r = _guild.guild_table(guild_id, "ratings")
    table_watched = _guild.guild_table(guild_id, "watched")
    items: list[dict] = []
    async with _connect() as db:
        async with db.execute(
            f"SELECT w.id, w.lot_name, w.detected_at, w.confidence, "
            f"  COALESCE(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as ratings_count "
            f"FROM {table_w} w LEFT JOIN {table_r} r ON r.winner_id = w.id "
            f"GROUP BY w.id ORDER BY w.detected_at DESC LIMIT ?", (limit,)
        ) as cur:
            for row in await cur.fetchall():
                items.append({
                    "type": "winner", "id": row[0], "name": row[1],
                    "ts": row[2], "confidence": row[3],
                    "avg_rating": round(row[4], 1) if row[4] else 0.0,
                    "ratings_count": row[5],
                })
        async with db.execute(
            f"SELECT id, title, watched_at, rating FROM {table_watched} ORDER BY watched_at DESC LIMIT ?", (limit,)
        ) as cur:
            for row in await cur.fetchall():
                items.append({
                    "type": "watched", "id": row[0], "name": row[1],
                    "ts": row[2], "rating": row[3]
                })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# --- Aliases: старые функции без g_ теперь работают с guild_id=0 (default) ---
# Это позволяет старому коду (web.py, bot.py до Фазы 2/3) продолжать работать

async def add_watched_v2(title: str, rating: int | None, watcher_user_id: int) -> bool:
    """v1.0.0 alias: add_watched для guild_id=0 (default)."""
    return await g_add_watched(0, title, rating, watcher_user_id)


# === v1.0.0 aliases: все старые функции работают с guild_id=0 (default) ===
# Это сохраняет обратную совместимость до полной миграции web.py/bot.py в Фазе 2/3

# --- watched aliases ---
async def add_watched(title: str, rating: int | None, watcher_user_id: int) -> bool:
    return await g_add_watched(0, title, rating, watcher_user_id)

async def is_watched(title: str) -> bool:
    return await g_is_watched(0, title)

async def list_watched(limit: int = 50) -> list[tuple]:
    return await g_list_watched(0, limit)

async def delete_watched(watched_id: int) -> bool:
    return await g_delete_watched(0, watched_id)


# --- quotes aliases ---
async def add_quote(author: str, author_user_id: int | None, text: str, recorded_by: int,
                   author_avatar_url: str | None = None, message_link: str | None = None) -> int:
    return await g_add_quote(0, author, author_user_id, text, recorded_by, author_avatar_url, message_link)

async def get_quote(quote_id: int) -> tuple | None:
    return await g_get_quote(0, quote_id)

async def list_quotes(limit: int = 50, offset: int = 0, search: str | None = None) -> list[tuple]:
    return await g_list_quotes(0, limit, offset, search)

async def count_quotes(search: str | None = None) -> int:
    return await g_count_quotes(0, search)

async def delete_quote(quote_id: int) -> bool:
    return await g_delete_quote(0, quote_id)

async def top_quotes(limit: int = 10) -> list[tuple]:
    return await g_top_quotes(0, limit)


# --- winners aliases ---
async def add_winner(lot_id: str | None, lot_name: str, tmdb_id: int | None,
                    confidence: str = "unconfirmed") -> int:
    return await g_add_winner(0, lot_id, lot_name, tmdb_id, confidence)

async def confirm_winner(winner_id: int, confirmed_by: int) -> bool:
    return await g_confirm_winner(0, winner_id, confirmed_by)

async def list_winners(limit: int = 20) -> list[tuple]:
    return await g_list_winners(0, limit)

async def delete_winner(winner_id: int) -> bool:
    return await g_delete_winner(0, winner_id)

async def get_winners_with_ratings(limit: int = 50) -> list[dict]:
    return await g_get_winners_with_ratings(0, limit)


# --- ratings aliases ---
async def upsert_rating(winner_id: int, user_discord_id: int, rating: int) -> bool:
    return await g_upsert_rating(0, winner_id, user_discord_id, rating)

async def get_average_rating(winner_id: int) -> tuple[float, int]:
    return await g_get_average_rating(0, winner_id)

async def get_user_rating(winner_id: int, user_discord_id: int) -> int | None:
    return await g_get_user_rating(0, winner_id, user_discord_id)

async def get_ratings_for_winner(winner_id: int) -> list[tuple]:
    table = _guild.guild_table(0, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT user_discord_id, rating, created_at, updated_at FROM {table} WHERE winner_id = ? ORDER BY created_at",
            (winner_id,)
        ) as cur:
            return await cur.fetchall()


# --- wheel_items aliases ---
async def add_wheel_item(name: str, tmdb_id: int | None, added_by: int) -> int:
    return await g_add_wheel_item(0, name, tmdb_id, added_by)

async def list_wheel_items(active_only: bool = True) -> list[dict]:
    return await g_list_wheel_items(0, active_only)

async def remove_wheel_item(item_id: int) -> bool:
    return await g_remove_wheel_item(0, item_id)

async def clear_wheel() -> int:
    return await g_clear_wheel(0)

async def reassign_colors() -> None:
    await g_reassign_colors(0)


# --- movie_nights aliases ---
async def add_movie_night(title: str | None, scheduled_at: datetime, created_by: int,
                         event_id: int | None = None) -> int:
    return await g_add_movie_night(0, title, scheduled_at, created_by, event_id)


# --- recent_activity alias ---
async def recent_activity(limit: int = 20) -> list[dict]:
    return await g_recent_activity(0, limit)


# --- get_recent_unconfirmed_winners alias (для совместимости, хотя /watched удалён) ---
async def get_recent_unconfirmed_winners(minutes: int = 5) -> list[tuple]:
    table = _guild.guild_table(0, "winners")
    cutoff = (datetime.utcnow().timestamp() - minutes * 60)
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, lot_name, tmdb_id FROM {table} WHERE confidence = 'unconfirmed' AND detected_at > ?",
            (cutoff_iso,)
        ) as cur:
            return await cur.fetchall()


# --- g_filmnights (сбор фильмов для киновечера) ---

async def g_start_filmnight(guild_id: int, started_by: int, max_per_user: int = 3) -> int | None:
    """Запустить новый сбор фильмов. Если уже есть активный — вернуть его id.
    Возвращает id активного filmnight или None если произошла ошибка.
    """
    table = _guild.guild_table(guild_id, "filmnights")
    async with _connect() as db:
        # Проверяем есть ли уже активный
        async with db.execute(
            f"SELECT id FROM {table} WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row[0]  # Уже активный — возвращаем существующий
        # Создаём новый
        cur = await db.execute(
            f"INSERT INTO {table} (status, max_per_user, started_by, started_at) "
            f"VALUES ('active', ?, ?, ?)",
            (max_per_user, started_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def g_get_active_filmnight(guild_id: int) -> dict | None:
    """Получить активный сбор фильмов или None."""
    table = _guild.guild_table(guild_id, "filmnights")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, status, max_per_user, started_by, started_at, completed_at, wheel_items_count "
            f"FROM {table} WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "status": row[1], "max_per_user": row[2],
            "started_by": row[3], "started_at": row[4],
            "completed_at": row[5], "wheel_items_count": row[6],
        }


async def g_complete_filmnight(guild_id: int, completed_by: int = 0) -> bool:
    """Завершить активный сбор (статус → completed).
    Записывает количество фильмов в колесе на момент завершения.
    """
    table = _guild.guild_table(guild_id, "filmnights")
    wheel_table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        # Считаем активные лоты колеса
        async with db.execute(f"SELECT COUNT(*) FROM {wheel_table} WHERE is_active = 1") as cur:
            count_row = await cur.fetchone()
        wheel_count = count_row[0] if count_row else 0
        cur = await db.execute(
            f"UPDATE {table} SET status = 'completed', completed_at = ?, completed_by = ?, wheel_items_count = ? "
            f"WHERE status = 'active'",
            (datetime.utcnow().isoformat(), completed_by, wheel_count),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_count_user_wheel_items(guild_id: int, user_discord_id: int) -> int:
    """Сколько активных фильмов в колесе от конкретного юзера.
    Используется для проверки лимита max_per_user.
    """
    table = _guild.guild_table(guild_id, "wheel_items")
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE added_by = ? AND is_active = 1",
            (user_discord_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# --- aliases для filmnight (guild_id=0) ---
async def start_filmnight(started_by: int, max_per_user: int = 3) -> int | None:
    return await g_start_filmnight(0, started_by, max_per_user)

async def get_active_filmnight() -> dict | None:
    return await g_get_active_filmnight(0)

async def complete_filmnight(completed_by: int = 0) -> bool:
    return await g_complete_filmnight(0, completed_by)

async def count_user_wheel_items(user_discord_id: int) -> int:
    return await g_count_user_wheel_items(0, user_discord_id)

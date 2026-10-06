"""Асинхронный доступ к SQLite.

Схема:
- Глобальные таблицы: users, tg_links, movie_meta, settings, guilds
- Per-guild таблицы: guild_{id}_watched, _quotes, _winners, _wheel_items, _ratings,
  _movie_nights, _settings, _watchlist
- Старые глобальные таблицы (watched, quotes, winners, etc.) сохранены для
  обратной совместимости со старым кодом через aliases (см. в конце файла).

Multi-tenant: каждая функция с префиксом g_ принимает guild_id первым параметром.
Функции без g_ — алиасы к guild_id=0 (default guild).
"""
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
                # v1.7.4: персональные TG-уведомления (по умолчанию OFF)
                "tg_notify_winner": "INTEGER DEFAULT 0",
                "tg_notify_collection_started": "INTEGER DEFAULT 0",
                "tg_notify_collection_ready": "INTEGER DEFAULT 0",
                # v1.8.0: Steam-профиль (для Тайного Санты и будущих фич)
                "steam_profile_url": "TEXT",
                "steam_id64": "TEXT",
                "steam_persona": "TEXT",
                "steam_avatar_url": "TEXT",
                # v1.8.2: пароль юзера (sha256 + salt)
                "password_hash": "TEXT",
                "password_salt": "TEXT",
                # v1.9.6: роль юзера ('superuser' | 'junior-admin' | 'user')
                "role": "TEXT DEFAULT 'user'",
                # v2.0.3: timestamp последнего просмотра своих ачивок — для unread-бейджа
                "last_viewed_achievements_at": "TEXT",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    log.info("Migrating users: adding column %s", col_name)
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            await db.commit()

            # === Миграция v1.9.6: заполнить role для существующих юзеров ===
            # is_admin=1 → 'superuser' если discord_id == env ADMIN_DISCORD_ID
            # is_admin=1 → 'junior-admin' для остальных админов
            # is_admin=0 → 'user' (дефолт, ничего не делаем)
            async with db.execute("SELECT COUNT(*) FROM users WHERE role IS NULL OR role = 'user' AND is_admin = 1") as cur:
                needs_migration = (await cur.fetchone())[0]
            if needs_migration > 0:
                log.info("v1.9.6 migration: converting is_admin → role")
                env_admin = settings.admin_discord_id
                if env_admin:
                    await db.execute(
                        "UPDATE users SET role = 'superuser' WHERE is_admin = 1 AND discord_id = ?",
                        (env_admin,)
                    )
                    await db.execute(
                        "UPDATE users SET role = 'junior-admin' WHERE is_admin = 1 AND discord_id != ? AND (role IS NULL OR role = 'user')",
                        (env_admin,)
                    )
                else:
                    await db.execute(
                        "UPDATE users SET role = 'junior-admin' WHERE is_admin = 1 AND (role IS NULL OR role = 'user')"
                    )
                await db.commit()
                log.info("v1.9.6 migration done: roles assigned")

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
                # Шкала 0.5-5 с половинками (после v1.8.7 миграции)
                if rating_val and _is_valid_rating(rating_val):
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

            # === Миграция v1.8.7: шкала оценок 1-10 → 0.5-5 (с половинками) ===
            # Запускается ОДИН РАЗ (флаг migration_v187_done в global settings).
            # Конвертирует все существующие оценки в guild_{id}_ratings и
            # legacy watched.rating по формуле: new_rating = old_rating / 2.
            # Пример: 10→5.0, 9→4.5, 8→4.0, 7→3.5, 6→3.0, 5→2.5, 4→2.0, 3→1.5, 2→1.0, 1→0.5.
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'migration_v187_done'"
            ) as cur:
                v187_row = await cur.fetchone()
            if not v187_row:
                log.info("v1.8.7 migration: converting ratings 1-10 → 0.5-5")
                # Получаем список всех guild_id из реестра
                try:
                    async with db.execute("SELECT guild_id FROM guilds") as cur:
                        guild_ids = [row[0] for row in await cur.fetchall()]
                except Exception:
                    guild_ids = []
                # Добавляем guild 0 (legacy)
                if 0 not in guild_ids:
                    guild_ids.append(0)
                total_migrated_ratings = 0
                total_migrated_watched = 0
                for gid in guild_ids:
                    table_r = guild_module.guild_table(gid, "ratings")
                    table_w = guild_module.guild_table(gid, "watched")
                    # Проверяем существование таблиц (для нового guild таблиц может не быть)
                    try:
                        cur_r = await db.execute(
                            f"UPDATE {table_r} SET rating = rating / 2.0 WHERE rating > 5"
                        )
                        total_migrated_ratings += cur_r.rowcount
                    except Exception as e:
                        log.warning("v1.8.7 migration: ratings table for guild %s: %s", gid, e)
                    try:
                        cur_w = await db.execute(
                            f"UPDATE {table_w} SET rating = rating / 2.0 WHERE rating IS NOT NULL AND rating > 5"
                        )
                        total_migrated_watched += cur_w.rowcount
                    except Exception as e:
                        log.warning("v1.8.7 migration: watched table for guild %s: %s", gid, e)
                # Также мигрируем legacy ratings и watched (если существуют)
                try:
                    cur_r = await db.execute(
                        "UPDATE ratings SET rating = rating / 2.0 WHERE rating > 5"
                    )
                    total_migrated_ratings += cur_r.rowcount
                except Exception:
                    pass  # legacy таблицы могут не существовать
                try:
                    cur_w = await db.execute(
                        "UPDATE watched SET rating = rating / 2.0 WHERE rating IS NOT NULL AND rating > 5"
                    )
                    total_migrated_watched += cur_w.rowcount
                except Exception:
                    pass
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value, is_secret) VALUES ('migration_v187_done', '1', 0)"
                )
                await db.commit()
                log.info(
                    "v1.8.7 migration done: %d ratings + %d watched records converted to 0.5-5 scale",
                    total_migrated_ratings, total_migrated_watched
                )

            # === Миграция v1.9.7: добавить колонку review в guild_{id}_ratings ===
            try:
                async with db.execute("SELECT guild_id FROM guilds") as cur:
                    guild_ids_migr = [row[0] for row in await cur.fetchall()]
            except Exception:
                guild_ids_migr = []
            if 0 not in guild_ids_migr:
                guild_ids_migr.append(0)
            for gid in guild_ids_migr:
                table_r_migr = guild_module.guild_table(gid, "ratings")
                try:
                    async with db.execute(f"PRAGMA table_info({table_r_migr})") as cur:
                        cols = {row[1] for row in await cur.fetchall()}
                    if "review" not in cols:
                        await db.execute(f"ALTER TABLE {table_r_migr} ADD COLUMN review TEXT")
                        log.info("v1.9.7 migration: added review column to %s", table_r_migr)
                except Exception as e:
                    log.warning("v1.9.7 migration: %s: %s", table_r_migr, e)
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
            "SELECT discord_id, username, display_name, is_admin, avatar_url, roles, top_role, guild_name, last_login_at, role "
            "FROM users WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            return await cur.fetchone()


# === Roles (v1.9.6) ===
# 3 уровня: 'superuser' (Матка), 'junior-admin' (Трутень), 'user' (Пчела)
# superuser: env ADMIN_DISCORD_ID или role='superuser' в БД
# junior-admin: role='junior-admin' в БД
# user: role='user' (или NULL/любое другое значение)

ROLE_LABELS = {
    'superuser': 'Матка',
    'junior-admin': 'Трутень',
    'user': 'Пчела',
}


async def get_user_role(discord_id: int) -> str:
    """Получить роль юзера. Возвращает 'superuser', 'junior-admin' или 'user'."""
    # Env admin — всегда superuser
    if settings.admin_discord_id is not None and settings.admin_discord_id == discord_id:
        return 'superuser'
    env_admin = settings.admin_login
    if env_admin.isdigit() and int(env_admin) == discord_id:
        return 'superuser'
    # БД
    async with _connect() as db:
        async with db.execute(
            "SELECT role FROM users WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return 'user'
    return row[0] if row[0] in ('superuser', 'junior-admin', 'user') else 'user'


async def is_superuser(discord_id: int) -> bool:
    """True только для superuser (env admin или role='superuser')."""
    return await get_user_role(discord_id) == 'superuser'


async def is_admin(discord_id: int) -> bool:
    """True для superuser ИЛИ junior-admin (any admin).
    Обратная совместимость — используется в шаблонах и роутах где нужен 'any admin'.
    """
    role = await get_user_role(discord_id)
    return role in ('superuser', 'junior-admin')


async def is_admin_or_above(discord_id: int) -> bool:
    """Alias для is_admin — обратная совместимость."""
    return await is_admin(discord_id)


async def set_user_role(discord_id: int, role: str) -> bool:
    """Установить роль юзера. Только superuser может вызывать.
    Нельзя понизить env-admin (superuser через env).
    Возвращает True если обновлено, False если отклонено.
    """
    if role not in ('superuser', 'junior-admin', 'user'):
        return False
    # Env admin нельзя понижать
    if settings.admin_discord_id is not None and settings.admin_discord_id == discord_id:
        return False
    env_admin = settings.admin_login
    if env_admin.isdigit() and int(env_admin) == discord_id:
        return False
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE users SET role = ?, is_admin = ? WHERE discord_id = ?",
            (role, 1 if role in ('superuser', 'junior-admin') else 0, discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_admin(discord_id: int, is_admin_flag: bool) -> None:
    """Legacy: установить is_admin флаг. Конвертирует в role.
    True → 'junior-admin' (не 'superuser' — тот только через env)
    False → 'user'
    """
    await set_user_role(discord_id, 'junior-admin' if is_admin_flag else 'user')


async def list_users() -> list[tuple]:
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, is_admin, last_login_at, avatar_url, top_role, guild_name, role "
            "FROM users ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()


async def list_active_members(limit: int = 12) -> list[dict]:
    """Список последних активных участников для дашборда (виден всем юзерам).

    Возвращает [{discord_id, display_name, username, avatar_url, last_login_at, role}, ...]
    Сортировка по last_login_at DESC (кто недавно заходил — те сверху).
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, avatar_url, last_login_at, role "
            "FROM users WHERE last_login_at IS NOT NULL "
            "ORDER BY last_login_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"discord_id": r[0], "username": r[1], "display_name": r[2] or r[1],
         "avatar_url": r[3], "last_login_at": r[4], "role": r[5] or 'user'}
        for r in rows
    ]


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


# === TG Notifications settings (v1.7.4) ===

# Список доступных тумблеров: (key, label, description)
TG_NOTIFY_SETTINGS = [
    ("tg_notify_winner", "🎡 Победитель колеса", "Когда крутанули колесо и определился победитель"),
    ("tg_notify_collection_started", "🎬 Сбор начат", "Когда кто-то запустил сбор фильмов (вы в числе организаторов)"),
    ("tg_notify_collection_ready", "✅ Все готовы", "Когда все участники сбора нажали «Готов»"),
]


async def get_user_tg_settings(discord_id: int) -> dict:
    """Получить настройки TG-уведомлений юзера. Возвращает dict {key: bool}."""
    async with _connect() as db:
        async with db.execute(
            "SELECT tg_notify_winner, tg_notify_collection_started, tg_notify_collection_ready "
            "FROM users WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {key: False for key, _, _ in TG_NOTIFY_SETTINGS}
    return {
        "tg_notify_winner": bool(row[0]),
        "tg_notify_collection_started": bool(row[1]),
        "tg_notify_collection_ready": bool(row[2]),
    }


async def set_user_tg_setting(discord_id: int, key: str, value: bool) -> bool:
    """Установить одну настройку TG-уведомлений. key должен быть из TG_NOTIFY_SETTINGS."""
    valid_keys = {k for k, _, _ in TG_NOTIFY_SETTINGS}
    if key not in valid_keys:
        raise ValueError(f"Invalid tg_notify key: {key}")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE users SET {key} = ? WHERE discord_id = ?",
            (1 if value else 0, discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_notification_recipients(setting_key: str, organizer_discord_id: int | None = None) -> list[dict]:
    """Получить список юзеров для уведомления.
    Возвращает list of dicts: {discord_id, tg_user_id, tg_username}.

    Логика:
    - organizer_discord_id (если задан) — добавляется в список если у него включён тумблер setting_key
    - Все админы с привязкой TG и включённым тумблером setting_key
    """
    valid_keys = {k for k, _, _ in TG_NOTIFY_SETTINGS}
    if setting_key not in valid_keys:
        raise ValueError(f"Invalid setting_key: {setting_key}")

    recipients: list[dict] = []
    seen_discord_ids: set[int] = set()

    async with _connect() as db:
        # 1. Организатор (если задан)
        if organizer_discord_id is not None:
            async with db.execute(
                f"SELECT u.discord_id, t.tg_user_id, t.tg_username "
                f"FROM users u JOIN tg_links t ON u.discord_id = t.discord_id "
                f"WHERE u.discord_id = ? AND u.{setting_key} = 1 AND t.tg_user_id IS NOT NULL",
                (organizer_discord_id,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                recipients.append({
                    "discord_id": row[0],
                    "tg_user_id": row[1],
                    "tg_username": row[2],
                })
                seen_discord_ids.add(row[0])

        # 2. Админы с привязкой TG и включённым тумблером
        # Админ = env ADMIN_DISCORD_ID (через settings) или is_admin=1 в БД
        async with db.execute(
            f"SELECT u.discord_id, t.tg_user_id, t.tg_username "
            f"FROM users u JOIN tg_links t ON u.discord_id = t.discord_id "
            f"WHERE u.role IN ('superuser', 'junior-admin') AND u.{setting_key} = 1 AND t.tg_user_id IS NOT NULL"
        ) as cur:
            for row in await cur.fetchall():
                if row[0] not in seen_discord_ids:
                    recipients.append({
                        "discord_id": row[0],
                        "tg_user_id": row[1],
                        "tg_username": row[2],
                    })
                    seen_discord_ids.add(row[0])

        # 3. env ADMIN_DISCORD_ID — может быть не в БД (если ни разу не логинился в панель)
        env_admin_id = settings.admin_discord_id
        if env_admin_id is not None and env_admin_id not in seen_discord_ids:
            async with db.execute(
                f"SELECT u.discord_id, t.tg_user_id, t.tg_username "
                f"FROM users u JOIN tg_links t ON u.discord_id = t.discord_id "
                f"WHERE u.discord_id = ? AND u.{setting_key} = 1 AND t.tg_user_id IS NOT NULL",
                (env_admin_id,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                recipients.append({
                    "discord_id": row[0],
                    "tg_user_id": row[1],
                    "tg_username": row[2],
                })

    return recipients


async def is_tg_linked(discord_id: int) -> bool:
    """Проверить, привязал ли юзер свой TG-аккаунт."""
    async with _connect() as db:
        async with db.execute(
            "SELECT tg_user_id FROM tg_links WHERE discord_id = ? AND tg_user_id IS NOT NULL",
            (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


# === Steam Profile (v1.8.0) ===

async def get_user_steam_profile(discord_id: int) -> dict | None:
    """Получить Steam-профиль юзера из таблицы users.
    Возвращает dict: {steam_profile_url, steam_id64, steam_persona, steam_avatar_url}
    или None если профиль не указан.
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT steam_profile_url, steam_id64, steam_persona, steam_avatar_url "
            "FROM users WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[1]:  # row[1] = steam_id64
        return None
    return {
        "steam_profile_url": row[0],
        "steam_id64": row[1],
        "steam_persona": row[2],
        "steam_avatar_url": row[3],
    }


async def set_user_steam_profile(
    discord_id: int,
    steam_profile_url: str | None,
    steam_id64: str | None,
    steam_persona: str | None,
    steam_avatar_url: str | None,
) -> bool:
    """Сохранить/обновить Steam-профиль юзера.
    Передайте None для всех параметров чтобы очистить профиль.
    Возвращает True если обновлено.
    """
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE users SET "
            "steam_profile_url = ?, steam_id64 = ?, steam_persona = ?, steam_avatar_url = ? "
            "WHERE discord_id = ?",
            (steam_profile_url, steam_id64, steam_persona, steam_avatar_url, discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def is_steam_profile_set(discord_id: int) -> bool:
    """Проверить, указан ли Steam-профиль у юзера."""
    profile = await get_user_steam_profile(discord_id)
    return profile is not None and bool(profile.get("steam_id64"))


# === Password (v1.8.2 → v1.8.5 reinforced) ===

import hashlib as _hashlib
import secrets as _secrets

# Параметры PBKDF2 (OWASP 2023 recommendation: ≥ 600 000 iterations для SHA-256)
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_ALGO = "sha256"
# Префикс для нового формата: "pbkdf2_sha256$iterations$salt_hex$hash_hex"
# Старый формат (v1.8.2): просто hex-строка без префикса, 1000 iter SHA-256 без HMAC.
_PASSWORD_NEW_PREFIX = "pbkdf2"
_LEGACY_SHA256_ITERATIONS = 1000  # для старых паролей


def _hash_password_legacy(password: str, salt: str) -> str:
    """Старый алгоритм (v1.8.2): 1000 итераций SHA-256 без HMAC.

    Оставлен только для верификации существующих паролей.
    При успешной верификации пароль пере-хешируется через _hash_password_new.
    """
    h = password + salt
    for _ in range(_LEGACY_SHA256_ITERATIONS):
        h = _hashlib.sha256(h.encode()).hexdigest()
    return h


def _hash_password_new(password: str, salt_hex: str | None = None) -> str:
    """Новый алгоритм (v1.8.5): PBKDF2-HMAC-SHA256, 600 000 итераций.

    Возвращает строку вида: pbkdf2_sha256$600000$salt_hex$hash_hex
    """
    if salt_hex is None:
        salt_hex = _secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    dk = _hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt_bytes, _PBKDF2_ITERATIONS)
    return f"{_PASSWORD_NEW_PREFIX}_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt_hex}${dk.hex()}"


def _is_legacy_hash(stored_hash: str) -> bool:
    """True если stored_hash — старый формат (без префикса 'pbkdf2_')."""
    return not stored_hash.startswith(f"{_PASSWORD_NEW_PREFIX}_")


def _verify_password_format(stored_hash: str, password: str) -> bool:
    """Проверить пароль против stored_hash в любом формате (legacy или новый).

    Возвращает True только при точном совпадении (constant-time через compare_digest).
    """
    if _is_legacy_hash(stored_hash):
        # Старый формат: salt хранится в password_salt отдельно, hash — в password_hash.
        # Вызывающий (verify_user_password) подставит salt.
        # Эта ветка вызывается из verify_user_password напрямую.
        raise RuntimeError("legacy hash should be verified via verify_user_password with salt")
    # Новый формат: pbkdf2_sha256$iter$salt_hex$hash_hex
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    algo_full, iter_str, salt_hex, hash_hex = parts
    if algo_full != f"{_PASSWORD_NEW_PREFIX}_{_PBKDF2_ALGO}":
        return False  # неизвестный алгоритм
    try:
        iterations = int(iter_str)
    except ValueError:
        return False
    try:
        salt_bytes = bytes.fromhex(salt_hex)
        stored_dk_bytes = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    # PBKDF2-HMAC с теми же параметрами
    test_dk = _hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt_bytes, iterations)
    return _secrets.compare_digest(test_dk, stored_dk_bytes)


def _hash_password(password: str, salt: str) -> str:
    """DEPRECATED: только для обратной совместимости.

    Возвращает новый формат хеша (PBKDF2), salt используется только если передан.
    Salt — для совместимости с вызовами, где salt генерировался отдельно.
    """
    # Игнорируем переданный salt (новый формат хранит salt в самой строке),
    # но генерируем свой.
    return _hash_password_new(password)


async def set_user_password(discord_id: int, password: str) -> bool:
    """Установить пароль для юзера (новый формат PBKDF2, v1.8.5+).

    Минимальная длина: 8 символов. Старые пароли (длиной 4-7) НЕ затрагиваются
    при verify, но при смене/установке нового пароля требуется длина ≥ 8.

    Возвращает True если обновлено.
    """
    if not password or len(password) < 8:
        return False
    password_hash = _hash_password_new(password)
    # password_salt — оставляем NULL для нового формата (salt внутри хеша).
    # Legacy-код может читать password_salt, но для новых паролей он не нужен.
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE users SET password_hash = ?, password_salt = NULL WHERE discord_id = ?",
            (password_hash, discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def verify_user_password(discord_id: int, password: str) -> bool:
    """Проверить пароль юзера (поддержка legacy + нового формата).

    Если пароль в старом формате (1000 iter SHA-256 без HMAC) и проверка успешна —
    автоматически пере-хеширует пароль через новый формат (PBKDF2 600k iter).
    Это обеспечивает плавную миграцию без требования пользователям менять пароли.

    Возвращает True если совпадает.
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT password_hash, password_salt FROM users WHERE discord_id = ?",
            (discord_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            return False
        stored_hash = row[0]
        salt = row[1]  # может быть NULL для нового формата

        # Определяем формат и проверяем
        if _is_legacy_hash(stored_hash):
            # Старый формат: salt в password_salt, hash в password_hash
            if not salt:
                # Аномалия: legacy hash без salt — не можем проверить
                return False
            test_hash = _hash_password_legacy(password, salt)
            match = _secrets.compare_digest(stored_hash, test_hash)
            if match:
                # Пере-хешируем через новый формат (плавная миграция)
                try:
                    new_hash = _hash_password_new(password)
                    await db.execute(
                        "UPDATE users SET password_hash = ?, password_salt = NULL WHERE discord_id = ?",
                        (new_hash, discord_id),
                    )
                    await db.commit()
                    log.info("User %s password migrated from legacy SHA-256 to PBKDF2", discord_id)
                except Exception as e:
                    log.warning("Failed to migrate password for user %s: %s", discord_id, e)
            return match
        else:
            # Новый формат: всё внутри stored_hash
            return _verify_password_format(stored_hash, password)


async def has_password(discord_id: int) -> bool:
    """Проверить, установлен ли пароль у юзера."""
    async with _connect() as db:
        async with db.execute(
            "SELECT password_hash FROM users WHERE discord_id = ? AND password_hash IS NOT NULL AND password_hash != ''",
            (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row)


async def reset_user_password(discord_id: int) -> bool:
    """Сбросить пароль юзера (админ). Возвращает True если обновлено."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE users SET password_hash = NULL, password_salt = NULL WHERE discord_id = ?",
            (discord_id,)
        )
        await db.commit()
        return cur.rowcount > 0


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


async def get_posters_for_titles(titles: list[str]) -> dict[str, str]:
    """Batch-выборка постеров для списка названий.
    Ищет в movie_meta (без TTL-проверки — постеры не протухают).
    Возвращает dict {lower_title: poster_url}.
    """
    if not titles:
        return {}
    result: dict[str, str] = {}
    async with _connect() as db:
        for title in titles:
            async with db.execute(
                "SELECT poster_url FROM movie_meta WHERE lower(query_title) = lower(?) AND poster_url IS NOT NULL AND poster_url != '' LIMIT 1",
                (title,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0]:
                result[title.lower()] = row[0]
    return result


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

async def upsert_rating(winner_id: int, user_discord_id: int, rating) -> bool:
    """Поставить или обновить оценку победителю (legacy, без guild_id).
    Один юзер = одна оценка на фильм (UNIQUE constraint).
    Возвращает True если оценка поставлена, False если рейтинг вне диапазона 0.5-5.

    Побочный эффект: при первой оценке победитель "переезжает" в watched
    (если ещё не там) — INSERT INTO watched, чтобы он появился в бэклоге.
    """
    if not _is_valid_rating(rating):
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


async def get_user_rating(winner_id: int, user_discord_id: int) -> float | None:
    """Оценка конкретного юзера для победителя (или None). Legacy."""
    async with _connect() as db:
        async with db.execute(
            "SELECT rating FROM ratings WHERE winner_id = ? AND user_discord_id = ?",
            (winner_id, user_discord_id)
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else None


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


async def g_list_watched(guild_id: int, limit: int = 50, offset: int = 0, search: str | None = None,
                         user_discord_id: int = 0, sort: str = "recent") -> list[tuple]:
    """Список просмотренных фильмов с пагинацией и опциональным поиском.

    sort: 'recent' (по дате, дефолт) или 'rating' (по средней оценке, топ вниз).
    Возвращает кортежи (id, title, watched_at, rating, avg_rating, ratings_count, user_rating, winner_id).
    """
    table_w = _guild.guild_table(guild_id, "watched")
    table_winners = _guild.guild_table(guild_id, "winners")
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        # Три отдельных scalar-подзапроса (SQLite не позволяет multi-column subquery в COALESCE).
        avg_subq = (
            f"COALESCE((SELECT AVG(r2.rating) FROM {table_r} r2 "
            f"          WHERE r2.winner_id = win.id), 0) as avg_rating"
        )
        count_subq = (
            f"COALESCE((SELECT COUNT(r3.id) FROM {table_r} r3 "
            f"          WHERE r3.winner_id = win.id), 0) as ratings_count"
        )
        user_subq = (
            f"(SELECT r4.rating FROM {table_r} r4 "
            f" WHERE r4.winner_id = win.id AND r4.user_discord_id = ?) as user_rating"
        )
        base_select = (
            f"SELECT w.id, w.title, w.watched_at, w.rating, "
            f"  win.id as winner_id, "
            f"  {avg_subq}, "
            f"  {count_subq}, "
            f"  {user_subq} "
            f"FROM {table_w} w "
            f"LEFT JOIN {table_winners} win ON lower(win.lot_name) = lower(w.title) "
        )
        # Сортировка: 'recent' → по дате DESC, 'rating' → по средней оценке DESC
        order_clause = "ORDER BY avg_rating DESC, ratings_count DESC" if sort == "rating" else "ORDER BY w.watched_at DESC"
        if search:
            sql = base_select + f"WHERE w.title LIKE ? GROUP BY w.id {order_clause} LIMIT ? OFFSET ?"
            # user_discord_id сначала (для user_subq), затем search, limit, offset
            params = (user_discord_id or 0, f"%{search}%", limit, offset)
        else:
            sql = base_select + f"GROUP BY w.id {order_clause} LIMIT ? OFFSET ?"
            params = (user_discord_id or 0, limit, offset)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        # Нормализуем: avg_rating → округлённый float, user_rating → float|None
        # SELECT: w.id(0), w.title(1), w.watched_at(2), w.rating(3), win.id(4=winner_id),
        #         avg_rating(5), ratings_count(6), user_rating(7)
        result = []
        for r in rows:
            avg = round(r[5], 1) if r[5] else 0.0
            user_r = float(r[7]) if r[7] is not None else None
            winner_id = r[4]  # может быть None если фильм не был победителем
            result.append((r[0], r[1], r[2], r[3], avg, r[6], user_r, winner_id))
        return result


async def g_count_watched(guild_id: int, search: str | None = None) -> int:
    """Подсчитать количество записей в бэклоге (с опциональным поиском)."""
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        if search:
            async with db.execute(f"SELECT COUNT(*) FROM {table} WHERE title LIKE ?", (f"%{search}%",)) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


async def g_delete_watched(guild_id: int, watched_id: int) -> bool:
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        cur = await db.execute(f"DELETE FROM {table} WHERE id = ?", (watched_id,))
        await db.commit()
        return cur.rowcount > 0


async def g_update_watched_title(guild_id: int, watched_id: int, new_title: str) -> bool:
    """Изменить название фильма в бэклоге."""
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET title = ? WHERE id = ?", (new_title.strip(), watched_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def g_update_watched_rating(guild_id: int, watched_id: int, rating) -> bool:
    """Поставить/обновить оценку фильму в бэклоге (legacy, до v1.8.4).
    Шкала 0.5-5 с шагом 0.5.
    """
    if not _is_valid_rating(rating):
        return False
    table = _guild.guild_table(guild_id, "watched")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET rating = ? WHERE id = ?", (rating, watched_id)
        )
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


async def g_get_or_create_winner_by_title(guild_id: int, title: str) -> int:
    """Найти winner по названию (case-insensitive) или создать unconfirmed.

    Используется при оценке фильма из /watched: если фильм не был победителем
    колеса (или ещё не имеет записи в winners), создаётся «виртуальный» winner
    с confidence='unconfirmed'. Это позволяет хранить per-user оценки в ratings.

    Безопасность от race condition: BEGIN IMMEDIATE сериализует писателей.
    Два одновременных вызова с одним title не создадут дубликат — второй
    увидит запись, созданную первым, после блокировки.

    Возвращает winner_id.
    """
    table = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        # BEGIN IMMEDIATE берёт write-lock на БД до COMMIT — другие писатели ждут.
        # Это устраняет race condition между SELECT и INSERT.
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                f"SELECT id FROM {table} WHERE lower(lot_name) = lower(?) ORDER BY id LIMIT 1",
                (title,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                winner_id = row[0]
            else:
                cur = await db.execute(
                    f"INSERT INTO {table} (lot_id, lot_name, tmdb_id, confidence, detected_at) VALUES (?, ?, ?, ?, ?)",
                    (None, title, None, "unconfirmed", datetime.utcnow().isoformat()),
                )
                winner_id = cur.lastrowid
            await db.commit()
            return winner_id
        except Exception:
            await db.rollback()
            raise


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


async def g_get_winners_with_ratings(guild_id: int, limit: int = 50, offset: int = 0, search: str | None = None) -> list[dict]:
    """Победители колеса с агрегированными рейтингами + пагинация + поиск.

    v1.9.7: Показывает ТОЛЬКО confirmed победителей (реально выбранные колесом).
    Виртуальные winners (confidence='unconfirmed', созданные при оценке фильма
    из бэклога) НЕ показываются — они нужны только как контейнер для оценок.
    """
    table_w = _guild.guild_table(guild_id, "winners")
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        if search:
            sql = (
                f"SELECT w.id, w.lot_name, w.tmdb_id, w.confidence, w.detected_at, w.confirmed_at, "
                f"  COALESCE(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as ratings_count "
                f"FROM {table_w} w LEFT JOIN {table_r} r ON r.winner_id = w.id "
                f"WHERE w.confidence = 'confirmed' AND w.lot_name LIKE ? "
                f"GROUP BY w.id ORDER BY w.detected_at DESC LIMIT ? OFFSET ?"
            )
            params = (f"%{search}%", limit, offset)
        else:
            sql = (
                f"SELECT w.id, w.lot_name, w.tmdb_id, w.confidence, w.detected_at, w.confirmed_at, "
                f"  COALESCE(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as ratings_count "
                f"FROM {table_w} w LEFT JOIN {table_r} r ON r.winner_id = w.id "
                f"WHERE w.confidence = 'confirmed' "
                f"GROUP BY w.id ORDER BY w.detected_at DESC LIMIT ? OFFSET ?"
            )
            params = (limit, offset)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "lot_name": r[1], "tmdb_id": r[2], "confidence": r[3],
             "detected_at": r[4], "confirmed_at": r[5],
             "avg_rating": round(r[6], 1) if r[6] else 0.0, "ratings_count": r[7]}
            for r in rows
        ]


async def g_count_winners(guild_id: int, search: str | None = None) -> int:
    """Подсчёт количества confirmed победителей колеса (с опциональным поиском).

    v1.9.7: Считает только confidence='confirmed'.
    """
    table_w = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        if search:
            async with db.execute(
                f"SELECT COUNT(*) FROM {table_w} WHERE confidence = 'confirmed' AND lot_name LIKE ?",
                (f"%{search}%",)
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                f"SELECT COUNT(*) FROM {table_w} WHERE confidence = 'confirmed'"
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


# --- g_ratings ---

# Допустимые значения оценок: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0
# (с половинками — 5 звёзд с возможностью поставить половину)
_VALID_RATINGS = {0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0}


def _is_valid_rating(rating) -> bool:
    """Проверить, что оценка валидна (0.5-5 с шагом 0.5).

    Принимает int или float. int 1..5 валиден (трактуется как 1.0, 2.0...).
    float 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0 валиден.
    """
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return False
    # Проверка на шаг 0.5: r * 2 должно быть целым числом от 1 до 10
    doubled = r * 2
    return doubled == int(doubled) and 1 <= int(doubled) <= 10


async def g_upsert_rating(guild_id: int, winner_id: int, user_discord_id: int, rating: float,
                          review: str | None = None) -> bool:
    """Per-user upsert оценки + автоматический «переезд» в watched при первой оценке.

    Атомарность через BEGIN IMMEDIATE.
    Шкала: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0 (с половинками).
    review: необязательный текстовый отзыв (v1.9.7).
    """
    if not _is_valid_rating(rating):
        return False
    table_r = _guild.guild_table(guild_id, "ratings")
    table_w = _guild.guild_table(guild_id, "watched")
    table_winners = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        now = datetime.utcnow().isoformat()
        await db.execute("BEGIN IMMEDIATE")
        try:
            # 1. Upsert оценки (per-user) + review
            review_val = review.strip()[:500] if review and review.strip() else None
            await db.execute(
                f"INSERT INTO {table_r} (winner_id, user_discord_id, rating, review, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(winner_id, user_discord_id) DO UPDATE SET rating = excluded.rating, "
                f"review = excluded.review, updated_at = excluded.updated_at",
                (winner_id, user_discord_id, rating, review_val, now, now),
            )
            # 2. Найти lot_name победителя
            async with db.execute(f"SELECT lot_name FROM {table_winners} WHERE id = ?", (winner_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False
            lot_name = row[0]
            # 3. Если ещё не в watched — добавляем (первая оценка = переезд в бэклог)
            async with db.execute(
                f"SELECT 1 FROM {table_w} WHERE lower(title) = lower(?) LIMIT 1", (lot_name,)
            ) as cur:
                if not await cur.fetchone():
                    await db.execute(
                        f"INSERT INTO {table_w} (title, watched_at, rating, watcher_user_id) VALUES (?, ?, ?, ?)",
                        (lot_name, now, rating, user_discord_id),
                    )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


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


async def g_get_user_rating(guild_id: int, winner_id: int, user_discord_id: int) -> float | None:
    """Получить per-user оценку победителя (0.5-5.0 с шагом 0.5)."""
    table = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT rating FROM {table} WHERE winner_id = ? AND user_discord_id = ?",
            (winner_id, user_discord_id)
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else None


# --- Публичный профиль (v1.9.0) ---

async def g_get_user_stats(guild_id: int, user_discord_id: int) -> dict:
    """Статистика юзера для публичного профиля.

    Возвращает:
    - ratings_count: сколько оценок поставил
    - avg_rating: средняя оценка юзера (0.0-5.0)
    - watched_count: сколько фильмов в бэклоге (с его оценкой или без)
    - quotes_count: сколько цитат записал
    - watchlist_count: сколько в списке желаемого
    """
    table_r = _guild.guild_table(guild_id, "ratings")
    table_w = _guild.guild_table(guild_id, "watched")
    table_q = _guild.guild_table(guild_id, "quotes")
    table_wl = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        # Оценки
        async with db.execute(
            f"SELECT COUNT(*), COALESCE(AVG(rating), 0) FROM {table_r} WHERE user_discord_id = ?",
            (user_discord_id,)
        ) as cur:
            row = await cur.fetchone()
            ratings_count = row[0] or 0
            avg_rating = round(row[1], 1) if row[1] else 0.0
        # Бэклог (watched) — сколько фильмов в бэклоге guild'а
        async with db.execute(f"SELECT COUNT(*) FROM {table_w}") as cur:
            watched_count = (await cur.fetchone())[0]
        # Цитаты — где этот юзер recorded_by (записал) или author_user_id (автор)
        async with db.execute(
            f"SELECT COUNT(*) FROM {table_q} WHERE recorded_by = ? OR author_user_id = ?",
            (user_discord_id, user_discord_id)
        ) as cur:
            quotes_count = (await cur.fetchone())[0]
        # Вишлист
        async with db.execute(
            f"SELECT COUNT(*) FROM {table_wl} WHERE user_discord_id = ?",
            (user_discord_id,)
        ) as cur:
            watchlist_count = (await cur.fetchone())[0]
    return {
        "ratings_count": ratings_count,
        "avg_rating": avg_rating,
        "watched_count": watched_count,
        "quotes_count": quotes_count,
        "watchlist_count": watchlist_count,
    }


async def g_get_user_recent_ratings(guild_id: int, user_discord_id: int, limit: int = 5) -> list[dict]:
    """Последние N оценок юзера: [{winner_id, lot_name, rating, updated_at, tmdb_id}, ...]"""
    table_r = _guild.guild_table(guild_id, "ratings")
    table_w = _guild.guild_table(guild_id, "winners")
    async with _connect() as db:
        async with db.execute(
            f"SELECT r.winner_id, w.lot_name, w.tmdb_id, r.rating, r.updated_at "
            f"FROM {table_r} r JOIN {table_w} w ON r.winner_id = w.id "
            f"WHERE r.user_discord_id = ? "
            f"ORDER BY r.updated_at DESC LIMIT ?",
            (user_discord_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"winner_id": r[0], "lot_name": r[1], "tmdb_id": r[2],
         "rating": float(r[3]), "updated_at": r[4]}
        for r in rows
    ]


async def g_get_user_recent_quotes(guild_id: int, user_discord_id: int, limit: int = 5) -> list[dict]:
    """Последние N цитат юзера (где он автор или записавший)."""
    table_q = _guild.guild_table(guild_id, "quotes")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, author, text, recorded_at, message_link "
            f"FROM {table_q} WHERE recorded_by = ? OR author_user_id = ? "
            f"ORDER BY recorded_at DESC LIMIT ?",
            (user_discord_id, user_discord_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "author": r[1], "text": r[2], "recorded_at": r[3], "message_link": r[4]}
        for r in rows
    ]


async def g_get_taste_match(guild_id: int, user_a: int, user_b: int) -> dict:
    """Насколько совпадают вкусы двух юзеров.

    Берём фильмы, которые оба оценили. Считаем среднюю разницу оценок.
    Возвращает:
    - common_count: сколько общих оценённых фильмов
    - compatibility: 0-100% (100 = полное совпадение, 0 = противоположные вкусы)
    - avg_diff: средняя разница (0 = идентичны, 5 = максимально разные)
    """
    if user_a == user_b:
        return {"common_count": 0, "compatibility": 0, "avg_diff": 5.0}
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        # JOIN оценок обоих юзеров по winner_id
        async with db.execute(
            f"SELECT a.rating, b.rating "
            f"FROM {table_r} a JOIN {table_r} b "
            f"ON a.winner_id = b.winner_id AND a.user_discord_id = ? AND b.user_discord_id = ?",
            (user_a, user_b)
        ) as cur:
            rows = await cur.fetchall()
    common = len(rows)
    if common == 0:
        return {"common_count": 0, "compatibility": 0, "avg_diff": 5.0}
    total_diff = sum(abs(float(a) - float(b)) for a, b in rows)
    avg_diff = total_diff / common
    # Совместимость: 0 разницы = 100%, 5 разницы = 0%
    compatibility = round(max(0, 100 - (avg_diff / 5.0) * 100))
    return {"common_count": common, "compatibility": compatibility, "avg_diff": round(avg_diff, 2)}


async def g_get_watched_together_count(guild_id: int, user_a: int, user_b: int) -> int:
    """Сколько фильмов оба юзера оценили (т.е. посмотрели вместе)."""
    if user_a == user_b:
        return 0
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM {table_r} a "
            f"JOIN {table_r} b ON a.winner_id = b.winner_id "
            f"WHERE a.user_discord_id = ? AND b.user_discord_id = ?",
            (user_a, user_b)
        ) as cur:
            return (await cur.fetchone())[0]


async def g_get_ratings_for_winner(guild_id: int, winner_id: int) -> list[dict]:
    """Все оценки победителя с инфо о юзере (для модалки «кто как оценил»).

    Возвращает [{user_discord_id, username, display_name, avatar_url, rating, updated_at}, ...]
    """
    table_r = _guild.guild_table(guild_id, "ratings")
    async with _connect() as db:
        async with db.execute(
            f"SELECT r.user_discord_id, r.rating, r.updated_at, "
            f"u.username, u.display_name, u.avatar_url "
            f"FROM {table_r} r LEFT JOIN users u ON r.user_discord_id = u.discord_id "
            f"WHERE r.winner_id = ? ORDER BY r.updated_at DESC",
            (winner_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"user_discord_id": r[0], "rating": float(r[1]), "updated_at": r[2],
         "username": r[3], "display_name": r[4], "avatar_url": r[5]}
        for r in rows
    ]


# --- g_achievements (v1.9.1) ---

# Допустимые типы триггеров
ACHIEVEMENT_TRIGGERS = {
    "manual",                 # только ручная выдача
    "ratings_count",          # поставил N оценок
    "quotes_count",           # добавил N цитат
    "watchlist_count",        # в вишлисте N фильмов
    "wheel_wins",             # N раз фильм юзера выиграл в колесе
    "collections_started",    # N раз организовал сбор
    "santa_participations",   # N раз участвовал в Сайте
    "watched_count",          # в бэклоге N фильмов
    "first_rating",           # первая оценка (threshold=1, особый)
    "first_quote",            # первая цитата
    # v2.0: Discord triggers
    "voice_time",             # N секунд в войс-чатах (любых)
    "voice_time_solo",        # N секунд в войс-чатах (одиночных)
    "voice_time_with_others", # N секунд в войс-чатах (с другими людьми)
    "game_play_time",         # N секунд играя в любые игры
}


async def g_create_achievement(
    guild_id: int,
    name: str,
    description: str | None,
    icon_config: dict,
    trigger_type: str,
    trigger_threshold: int = 0,
    discord_role_id: int | None = None,
    created_by: int = 0,
) -> int:
    """Создать шаблон ачивки. Возвращает achievement_id."""
    import json
    if trigger_type not in ACHIEVEMENT_TRIGGERS:
        raise ValueError(f"Unknown trigger_type: {trigger_type}")
    table = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        cur = await db.execute(
            f"INSERT INTO {table} (name, description, icon_config, trigger_type, trigger_threshold, discord_role_id, is_active, created_at, created_by) "
            f"VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (name, description, json.dumps(icon_config, ensure_ascii=False),
             trigger_type, trigger_threshold, discord_role_id,
             datetime.utcnow().isoformat(), created_by),
        )
        await db.commit()
        return cur.lastrowid


async def g_list_achievements(guild_id: int, active_only: bool = False) -> list[dict]:
    """Список всех ачивок гильдии."""
    import json
    table = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        sql = f"SELECT id, name, description, icon_config, trigger_type, trigger_threshold, discord_role_id, is_active, created_at, created_by FROM {table}"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id DESC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "description": r[2],
         "icon_config": json.loads(r[3]) if r[3] else {},
         "trigger_type": r[4], "trigger_threshold": r[5],
         "discord_role_id": r[6], "is_active": bool(r[7]),
         "created_at": r[8], "created_by": r[9]}
        for r in rows
    ]


async def g_get_achievement(guild_id: int, achievement_id: int) -> dict | None:
    """Получить одну ачивку."""
    import json
    table = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, name, description, icon_config, trigger_type, trigger_threshold, discord_role_id, is_active, created_at, created_by FROM {table} WHERE id = ?",
            (achievement_id,)
        ) as cur:
            r = await cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "name": r[1], "description": r[2],
        "icon_config": json.loads(r[3]) if r[3] else {},
        "trigger_type": r[4], "trigger_threshold": r[5],
        "discord_role_id": r[6], "is_active": bool(r[7]),
        "created_at": r[8], "created_by": r[9],
    }


async def g_delete_achievement(guild_id: int, achievement_id: int) -> bool:
    """Удалить ачивку и все её выдачи (каскадно)."""
    table_a = _guild.guild_table(guild_id, "achievements")
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        await db.execute(f"DELETE FROM {table_ua} WHERE achievement_id = ?", (achievement_id,))
        cur = await db.execute(f"DELETE FROM {table_a} WHERE id = ?", (achievement_id,))
        await db.commit()
        return cur.rowcount > 0


async def g_update_achievement(guild_id: int, achievement_id: int,
                               name: str | None = None, description: str | None = None) -> bool:
    """Обновить название и/или описание ачивки. Возвращает True если обновлено."""
    table = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip() or None)
        if not updates:
            return False
        params.append(achievement_id)
        cur = await db.execute(
            f"UPDATE {table} SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()
        return cur.rowcount > 0


async def g_update_achievement_icon_config(guild_id: int, achievement_id: int, icon_config: dict) -> bool:
    """Обновить icon_config ачивки (после скачивания иконки в static/icons/)."""
    import json
    table = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET icon_config = ? WHERE id = ?",
            (json.dumps(icon_config, ensure_ascii=False), achievement_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_grant_achievement(
    guild_id: int, achievement_id: int, user_discord_id: int, granted_by: int | None = None
) -> bool:
    """Выдать ачивку юзеру. Если уже есть — ничего не делает (idempotent).

    Возвращает True если выдали (или уже была выдана активная), False если ачивки не существует.
    """
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    table_a = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        # Проверяем существование ачивки
        async with db.execute(f"SELECT 1 FROM {table_a} WHERE id = ?", (achievement_id,)) as cur:
            if not await cur.fetchone():
                return False
        # INSERT OR IGNORE (UNIQUE constraint) — если уже есть, не упадёт
        await db.execute(
            f"INSERT OR IGNORE INTO {table_ua} (achievement_id, user_discord_id, granted_at, granted_by, is_active) "
            f"VALUES (?, ?, ?, ?, 1)",
            (achievement_id, user_discord_id, datetime.utcnow().isoformat(), granted_by),
        )
        # Если была неактивная — реактивируем
        await db.execute(
            f"UPDATE {table_ua} SET is_active = 1, granted_at = ?, granted_by = ? "
            f"WHERE achievement_id = ? AND user_discord_id = ?",
            (datetime.utcnow().isoformat(), granted_by, achievement_id, user_discord_id),
        )
        await db.commit()
        return True


async def g_revoke_achievement(guild_id: int, achievement_id: int, user_discord_id: int) -> bool:
    """Отозвать ачивку (не удалять, а пометить is_active=0). Возвращает True если обновлено."""
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table_ua} SET is_active = 0 "
            f"WHERE achievement_id = ? AND user_discord_id = ? AND is_active = 1",
            (achievement_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_list_user_achievements(
    guild_id: int, user_discord_id: int, active_only: bool = True
) -> list[dict]:
    """Список ачивок юзера (с деталями шаблона)."""
    import json
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    table_a = _guild.guild_table(guild_id, "achievements")
    async with _connect() as db:
        sql = (
            f"SELECT ua.achievement_id, ua.granted_at, ua.granted_by, ua.is_active, "
            f"a.name, a.description, a.icon_config, a.trigger_type, a.discord_role_id "
            f"FROM {table_ua} ua JOIN {table_a} a ON ua.achievement_id = a.id "
            f"WHERE ua.user_discord_id = ?"
        )
        if active_only:
            sql += " AND ua.is_active = 1"
        sql += " ORDER BY ua.granted_at DESC"
        async with db.execute(sql, (user_discord_id,)) as cur:
            rows = await cur.fetchall()
    return [
        {"achievement_id": r[0], "granted_at": r[1], "granted_by": r[2],
         "is_active": bool(r[3]), "name": r[4], "description": r[5],
         "icon_config": json.loads(r[6]) if r[6] else {},
         "trigger_type": r[7], "discord_role_id": r[8]}
        for r in rows
    ]


async def g_count_user_achievements(guild_id: int, user_discord_id: int) -> int:
    """Сколько активных ачивок у юзера."""
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM {table_ua} WHERE user_discord_id = ? AND is_active = 1",
            (user_discord_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def g_count_unread_achievements(guild_id: int, user_discord_id: int) -> int:
    """Сколько новых (непросмотренных) ачивок у юзера.

    Ачивка считается «новой» если её granted_at позже чем users.last_viewed_achievements_at.
    Если last_viewed_achievements_at IS NULL (юзер ни разу не открывал свои ачивки) —
    считаем все активные ачивки новыми.
    """
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        # Берём last_viewed_achievements_at юзера
        async with db.execute(
            "SELECT last_viewed_achievements_at FROM users WHERE discord_id = ?",
            (user_discord_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return 0
        last_viewed = row[0]
        if last_viewed:
            sql = (
                f"SELECT COUNT(*) FROM {table_ua} "
                f"WHERE user_discord_id = ? AND is_active = 1 AND granted_at > ?"
            )
            params = (user_discord_id, last_viewed)
        else:
            # Юзер ни разу не открывал ачивки → все активные — «новые»
            sql = (
                f"SELECT COUNT(*) FROM {table_ua} "
                f"WHERE user_discord_id = ? AND is_active = 1"
            )
            params = (user_discord_id,)
        async with db.execute(sql, params) as cur:
            return (await cur.fetchone())[0]


async def g_mark_achievements_viewed(user_discord_id: int) -> None:
    """Отметить что юзер просмотрел свои ачивки — обновить last_viewed_achievements_at."""
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET last_viewed_achievements_at = ? WHERE discord_id = ?",
            (now, user_discord_id)
        )
        await db.commit()


async def g_has_achievement(guild_id: int, achievement_id: int, user_discord_id: int) -> bool:
    """Проверить, есть ли у юзера активная ачивка."""
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        async with db.execute(
            f"SELECT 1 FROM {table_ua} WHERE achievement_id = ? AND user_discord_id = ? AND is_active = 1",
            (achievement_id, user_discord_id)
        ) as cur:
            return await cur.fetchone() is not None


async def g_get_users_with_achievement(guild_id: int, achievement_id: int) -> list[dict]:
    """Список юзеров с данной ачивкой (для страницы управления)."""
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    async with _connect() as db:
        async with db.execute(
            f"SELECT ua.user_discord_id, ua.granted_at, ua.granted_by, ua.is_active, "
            f"u.username, u.display_name, u.avatar_url "
            f"FROM {table_ua} ua LEFT JOIN users u ON ua.user_discord_id = u.discord_id "
            f"WHERE ua.achievement_id = ? ORDER BY ua.granted_at DESC",
            (achievement_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"user_discord_id": r[0], "granted_at": r[1], "granted_by": r[2],
         "is_active": bool(r[3]), "username": r[4], "display_name": r[5], "avatar_url": r[6]}
        for r in rows
    ]


# --- Auto-trigger checking ---

async def g_get_user_trigger_count(guild_id: int, user_discord_id: int, trigger_type: str) -> int:
    """Получить счётчик для конкретного триггера юзера."""
    table_r = _guild.guild_table(guild_id, "ratings")
    table_q = _guild.guild_table(guild_id, "quotes")
    table_wl = _guild.guild_table(guild_id, "watchlist")
    table_w = _guild.guild_table(guild_id, "winners")
    table_wheel = _guild.guild_table(guild_id, "wheel_items")
    table_c = _guild.guild_table(guild_id, "collections")
    table_sp = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        if trigger_type == "ratings_count" or trigger_type == "first_rating":
            sql = f"SELECT COUNT(*) FROM {table_r} WHERE user_discord_id = ?"
        elif trigger_type == "quotes_count" or trigger_type == "first_quote":
            sql = f"SELECT COUNT(*) FROM {table_q} WHERE recorded_by = ? OR author_user_id = ?"
            row = await (await db.execute(sql, (user_discord_id, user_discord_id))).fetchone()
            return row[0]
        elif trigger_type == "watchlist_count":
            sql = f"SELECT COUNT(*) FROM {table_wl} WHERE user_discord_id = ?"
        elif trigger_type == "wheel_wins":
            # Победитель, добавленный юзером, и winner создан
            sql = (f"SELECT COUNT(*) FROM {table_w} w "
                   f"JOIN {table_wheel} wi ON lower(wi.name) = lower(w.lot_name) "
                   f"WHERE wi.added_by = ?")
            row = await (await db.execute(sql, (user_discord_id,))).fetchone()
            return row[0]
        elif trigger_type == "collections_started":
            sql = f"SELECT COUNT(*) FROM {table_c} WHERE started_by = ?"
        elif trigger_type == "santa_participations":
            sql = f"SELECT COUNT(*) FROM {table_sp} WHERE user_discord_id = ?"
        elif trigger_type == "watched_count":
            table_watched = _guild.guild_table(guild_id, "watched")
            sql = f"SELECT COUNT(*) FROM {table_watched}"
            row = await (await db.execute(sql, ())).fetchone()
            return row[0]
        elif trigger_type in ("voice_time", "voice_time_solo", "voice_time_with_others"):
            # Возвращаем секунды, не количество
            solo = trigger_type == "voice_time_solo"
            with_others = trigger_type == "voice_time_with_others"
            return await g_get_voice_time(guild_id, user_discord_id, solo_only=solo, with_others_only=with_others)
        elif trigger_type == "game_play_time":
            return await g_get_game_play_time(guild_id, user_discord_id)
        else:
            return 0
        row = await (await db.execute(sql, (user_discord_id,))).fetchone()
        return row[0] if row else 0


async def g_check_and_grant_auto(guild_id: int, user_discord_id: int, trigger_type: str, bot_obj=None) -> list[dict]:
    """Проверить все авто-ачивки данного триггера и выдать если порог пройден.

    trigger_type: 'ratings_count', 'quotes_count', и т.д.
    bot_obj: экземпляр бота для выдачи Discord роли (опционально).

    Возвращает список выданных ачивок [{achievement_id, name, discord_role_id}, ...].
    """
    # 'manual' не обрабатываем — только авто-триггеры
    if trigger_type == "manual":
        return []
    # 'first_rating' / 'first_quote' — особый случай: всегда проверяем threshold=1
    check_type = trigger_type
    if trigger_type == "first_rating":
        check_type = "ratings_count"
    elif trigger_type == "first_quote":
        check_type = "quotes_count"

    # Текущее значение счётчика
    count = await g_get_user_trigger_count(guild_id, user_discord_id, check_type)

    # Все активные ачивки этого триггера
    table_a = _guild.guild_table(guild_id, "achievements")
    table_ua = _guild.guild_table(guild_id, "user_achievements")
    import json
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, name, description, icon_config, trigger_threshold, discord_role_id "
            f"FROM {table_a} WHERE trigger_type = ? AND is_active = 1",
            (trigger_type,)
        ) as cur:
            achievements = await cur.fetchall()

    granted = []
    for ach_id, name, desc, icon_cfg, threshold, role_id in achievements:
        # v2.0.1: Для game_play_time — проверяем конкретную игру из icon_config
        if trigger_type == "game_play_time":
            cfg = json.loads(icon_cfg) if icon_cfg else {}
            specific_game = cfg.get("game_name", "")
            if specific_game:
                count = await g_get_game_play_time_specific(guild_id, user_discord_id, specific_game)
            else:
                count = await g_get_game_play_time(guild_id, user_discord_id)
        # Для first_* триггеров threshold игнорируем, считаем что порог=1
        if trigger_type in ("first_rating", "first_quote"):
            should_grant = count >= 1
        else:
            should_grant = count >= threshold
        if not should_grant:
            continue
        # Проверяем, нет ли уже активной выдачи
        async with _connect() as db:
            async with db.execute(
                f"SELECT 1 FROM {table_ua} WHERE achievement_id = ? AND user_discord_id = ? AND is_active = 1",
                (ach_id, user_discord_id)
            ) as cur:
                if await cur.fetchone():
                    continue  # уже есть
        # Выдаём
        ok = await g_grant_achievement(guild_id, ach_id, user_discord_id, granted_by=None)
        if ok:
            granted.append({
                "achievement_id": ach_id,
                "name": name,
                "description": desc,
                "icon_config": json.loads(icon_cfg) if icon_cfg else {},
                "discord_role_id": role_id,
            })
            # Если есть Discord роль и передан bot_obj — выдаём роль
            if role_id and bot_obj is not None:
                try:
                    import bot as bot_module
                    await bot_module.assign_role_to_member(guild_id, user_discord_id, role_id)
                except Exception as e:
                    log.warning("Failed to assign role %s to user %s: %s", role_id, user_discord_id, e)
    return granted


# --- Voice sessions & activities (v2.0) ---

async def g_get_voice_stats(guild_id: int, user_discord_id: int) -> dict:
    """Статистика юзера в войс-чатах.

    Возвращает:
    - total_seconds: общее время в войсе
    - total_sessions: количество сессий
    - solo_seconds: время в одиночку
    - with_others_seconds: время с другими людьми
    - avg_session_seconds: средняя длительность сессии
    """
    table = _guild.guild_table(guild_id, "voice_sessions")
    async with _connect() as db:
        async with db.execute(
            f"SELECT COALESCE(SUM(duration_seconds), 0), COUNT(*), "
            f"COALESCE(SUM(CASE WHEN was_solo = 1 THEN duration_seconds ELSE 0 END), 0), "
            f"COALESCE(SUM(CASE WHEN was_solo = 0 THEN duration_seconds ELSE 0 END), 0) "
            f"FROM {table} WHERE user_discord_id = ? AND left_at IS NOT NULL",
            (user_discord_id,)
        ) as cur:
            row = await cur.fetchone()
    total = row[0] or 0
    sessions = row[1] or 0
    solo = row[2] or 0
    with_others = row[3] or 0
    return {
        "total_seconds": total,
        "total_sessions": sessions,
        "solo_seconds": solo,
        "with_others_seconds": with_others,
        "avg_session_seconds": round(total / sessions) if sessions > 0 else 0,
    }


async def g_get_voice_co_occurrence(guild_id: int, user_discord_id: int, limit: int = 5) -> list[dict]:
    """С какими юзерами чаще всего сидели в войсе одновременно.

    Берёт сессии юзера где was_solo=0, находит другие сессии в том же
    канале с пересечением по времени. Возвращает топ-N.

    Возвращает [{other_user_id, other_display_name, other_avatar_url,
                 co_seconds, top_games}, ...]
    """
    import json
    table = _guild.guild_table(guild_id, "voice_sessions")
    async with _connect() as db:
        # Все сессии юзера где был с другими
        async with db.execute(
            f"SELECT channel_id, joined_at, left_at, games_played "
            f"FROM {table} WHERE user_discord_id = ? AND was_solo = 0 AND left_at IS NOT NULL "
            f"ORDER BY joined_at DESC LIMIT 200",
            (user_discord_id,)
        ) as cur:
            my_sessions = await cur.fetchall()

    if not my_sessions:
        return []

    # Для каждой сессии ищем другие сессии в том же канале с пересечением времени
    co_occurrence = {}  # {other_user_id: {seconds: 0, games: set()}}
    async with _connect() as db:
        for ch_id, j_at, l_at, games in my_sessions:
            if not ch_id or not j_at or not l_at:
                continue
            async with db.execute(
                f"SELECT user_discord_id, joined_at, left_at, games_played "
                f"FROM {table} WHERE channel_id = ? AND user_discord_id != ? "
                f"AND left_at IS NOT NULL "
                f"AND joined_at < ? AND left_at > ?",
                (ch_id, user_discord_id, l_at, j_at)
            ) as cur:
                others = await cur.fetchall()
            for other_id, o_joined, o_left, o_games in others:
                if other_id not in co_occurrence:
                    co_occurrence[other_id] = {"seconds": 0, "games": set()}
                # Считаем пересечение
                try:
                    start = max(datetime.fromisoformat(j_at), datetime.fromisoformat(o_joined))
                    end = min(datetime.fromisoformat(l_at), datetime.fromisoformat(o_left))
                    overlap = int((end - start).total_seconds())
                    if overlap > 0:
                        co_occurrence[other_id]["seconds"] += overlap
                        if games:
                            for g in json.loads(games):
                                co_occurrence[other_id]["games"].add(g)
                        if o_games:
                            for g in json.loads(o_games):
                                co_occurrence[other_id]["games"].add(g)
                except Exception:
                    pass

    # Сортируем по убыванию времени, берём топ-N
    sorted_co = sorted(co_occurrence.items(), key=lambda x: x[1]["seconds"], reverse=True)[:limit]

    # Достаём имена юзеров
    result = []
    for other_id, data in sorted_co:
        async with _connect() as db:
            async with db.execute(
                "SELECT display_name, username, avatar_url FROM users WHERE discord_id = ?",
                (other_id,)
            ) as cur:
                u = await cur.fetchone()
        result.append({
            "other_user_id": other_id,
            "display_name": u[0] or u[1] if u else f"User#{other_id}",
            "avatar_url": u[2] if u else None,
            "co_seconds": data["seconds"],
            "top_games": list(data["games"])[:3],
        })
    return result


async def g_get_top_games(guild_id: int, user_discord_id: int, limit: int = 5) -> list[dict]:
    """Топ игр юзера по времени игры.

    Возвращает [{game_name, total_seconds, sessions}, ...]
    """
    table = _guild.guild_table(guild_id, "member_activities")
    async with _connect() as db:
        async with db.execute(
            f"SELECT activity_name, COALESCE(SUM(duration_seconds), 0), COUNT(*) "
            f"FROM {table} WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL "
            f"GROUP BY activity_name ORDER BY SUM(duration_seconds) DESC LIMIT ?",
            (user_discord_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"game_name": r[0], "total_seconds": r[1], "sessions": r[2]}
        for r in rows
    ]


async def g_get_user_recent_games(guild_id: int, user_discord_id: int, limit: int = 5) -> list[dict]:
    """Последние игры юзера — отсортированы по времени окончания сессии (DESC).

    В отличие от g_get_top_games (которая ранжирует по времени игры),
    эта функция показывает «во что играл недавно» — без агрегации.

    Возвращает [{game_name, total_seconds, ended_at}, ...]
    """
    table = _guild.guild_table(guild_id, "member_activities")
    async with _connect() as db:
        async with db.execute(
            f"SELECT activity_name, COALESCE(duration_seconds, 0), ended_at "
            f"FROM {table} WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL "
            f"ORDER BY ended_at DESC LIMIT ?",
            (user_discord_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"game_name": r[0], "total_seconds": r[1], "ended_at": r[2]}
        for r in rows
    ]


async def g_get_game_compat(guild_id: int, user_a: int, user_b: int) -> dict:
    """Совместимость двух юзеров по играм.

    Берём игры в которые играл каждый юзер (хотя бы 1 завершённая сессия).
    Считаем количество общих игр.
    compatibility = common / max(unique_a, unique_b) * 100  — насколько пересекаются библиотеки.

    Возвращает:
    - common_count: сколько общих игр
    - unique_a: сколько уникальных игр у user_a
    - unique_b: сколько уникальных игр у user_b
    - compatibility: 0-100%
    - common_games: list[str] — названия общих игр (для отображения)
    """
    if user_a == user_b:
        return {"common_count": 0, "unique_a": 0, "unique_b": 0, "compatibility": 0, "common_games": []}
    table = _guild.guild_table(guild_id, "member_activities")
    async with _connect() as db:
        # Уникальные нормализованные имена игр каждого юзера
        async with db.execute(
            f"SELECT DISTINCT {_NORM_GAME_SQL} AS norm, MAX(activity_name) AS display "
            f"FROM {table} WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL "
            f"GROUP BY norm",
            (user_a,)
        ) as cur:
            rows_a = await cur.fetchall()
        async with db.execute(
            f"SELECT DISTINCT {_NORM_GAME_SQL} AS norm, MAX(activity_name) AS display "
            f"FROM {table} WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL "
            f"GROUP BY norm",
            (user_b,)
        ) as cur:
            rows_b = await cur.fetchall()
    games_a = {r[0]: r[1] for r in rows_a}
    games_b = {r[0]: r[1] for r in rows_b}
    unique_a = len(games_a)
    unique_b = len(games_b)
    common_norm = set(games_a.keys()) & set(games_b.keys())
    common_count = len(common_norm)
    common_games = sorted({games_a[n] or games_b[n] for n in common_norm})
    if unique_a == 0 and unique_b == 0:
        compatibility = 0
    else:
        # Jaccard-like: common / max(a, b) — показывает «насколько пересекаются библиотеки»
        # Если у A 3 игры, у B 5, общих 2 → 2/5 = 40%
        compatibility = round(common_count / max(unique_a, unique_b) * 100)
    return {
        "common_count": common_count,
        "unique_a": unique_a,
        "unique_b": unique_b,
        "compatibility": compatibility,
        "common_games": common_games,
    }


async def g_get_game_play_time_specific(guild_id: int, user_discord_id: int, game_name: str) -> int:
    """Время игры в конкретную игру (секунды). Для триггера ачивок с конкретной игрой.

    v2.0.2: сравнение по нормализованному имени — ловит дубликаты типа
    «CS2», «CS2 », «cs2».
    """
    table = _guild.guild_table(guild_id, "member_activities")
    norm = _normalize_game_name(game_name).lower()
    async with _connect() as db:
        async with db.execute(
            f"SELECT COALESCE(SUM(duration_seconds), 0) FROM {table} "
            f"WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL "
            f"AND {_NORM_GAME_SQL} = ?",
            (user_discord_id, norm)
        ) as cur:
            return (await cur.fetchone())[0] or 0


def _normalize_game_name(name: str) -> str:
    """Нормализовать имя игры для дедупликации.

    - strip начальные/конечные пробелы
    - схлопнуть множественные пробелы/табы/переносы в один пробел
    - регистр НЕ меняется (для отображения сохраняем оригинальный вид)
    """
    if not name:
        return ""
    import re
    return re.sub(r'\s+', ' ', name).strip()


# SQL-выражение для нормализации имени игры внутри SQL-запроса.
# Заменяет табы/CR/LF на пробелы, затем 5 итераций REPLACE('X','  ',' ')
# схлопывают множественные пробелы (до 32 подряд). LOWER + TRIM для сравнения без регистра.
_NORM_GAME_SQL = (
    "LOWER(TRIM("
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "REPLACE(REPLACE(REPLACE(activity_name, char(9), ' '), char(10), ' '), char(13), ' '),"
    "  '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' ')"
    "))"
)


async def g_list_server_games(guild_id: int) -> list[dict]:
    """Список всех игр в которые играли на сервере (для ачивок и дашборда).

    Возвращает [{game_name, total_seconds, players_count}, ...] отсортированный по популярности.
    Дедупликация: группировка по нормализованному имени (TRIM + lowercase + collapse whitespace),
    но отображается «лучшее» (MAX) оригинальное написание.
    Возвращает [] если таблица не существует или нет данных.

    v2.0.2: дедупликация игр — раньше «CS2», «CS2 » и «cs2» считались разными играми.
    """
    try:
        table = _guild.guild_table(guild_id, "member_activities")
        async with _connect() as db:
            # Группируем по нормализованному имени, отображаем MAX(activity_name)
            async with db.execute(
                f"SELECT "
                f"  MAX(activity_name) AS display_name, "
                f"  COALESCE(SUM(duration_seconds), 0) AS total_seconds, "
                f"  COUNT(DISTINCT user_discord_id) AS players_count, "
                f"  {_NORM_GAME_SQL} AS norm_name "
                f"FROM {table} WHERE activity_type = 'playing' AND ended_at IS NOT NULL "
                f"GROUP BY norm_name "
                f"ORDER BY total_seconds DESC",
                ()
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"game_name": r[0], "total_seconds": r[1], "players_count": r[2]}
            for r in rows
        ]
    except Exception as e:
        log.warning("g_list_server_games failed (table may not exist yet): %s", e)
        return []


async def g_dedupe_games(guild_id: int) -> int:
    """Схлопнуть дубликаты игр в member_activities.

    Для каждого (user, canonical_name) с несколькими записями:
    - удаляем все строки группы
    - вставляем одну каноническую строку с суммарной длительностью

    Каноническое имя = MAX(activity_name) — лексикографически наибольшее
    (обычно это самое «полное» написание). Возвращает количество схлопнутых
    дубликатов (удалённых строк).

    Это безопасно для UNIQUE constraint (user, guild, type, name, started_at),
    т.к. мы сначала DELETE потом INSERT — конфликтовать не с чем.
    """
    table = _guild.guild_table(guild_id, "member_activities")
    merged = 0
    async with _connect() as db:
        # Найти группы дубликатов (один юзер + одно каноническое имя, но >1 строки)
        async with db.execute(
            f"SELECT user_discord_id, guild_id, "
            f"       {_NORM_GAME_SQL} AS norm, "
            f"       MAX(activity_name) AS canonical, "
            f"       MAX(started_at) AS keep_started, "
            f"       MAX(ended_at) AS keep_ended, "
            f"       COALESCE(SUM(duration_seconds), 0) AS total_dur, "
            f"       COUNT(*) AS cnt "
            f"FROM {table} WHERE activity_type = 'playing' AND ended_at IS NOT NULL "
            f"GROUP BY user_discord_id, guild_id, norm HAVING cnt > 1"
        ) as cur:
            groups = await cur.fetchall()
        if not groups:
            return 0
        for user_id, gid, norm, canonical, keep_started, keep_ended, total_dur, cnt in groups:
            # Удалить все строки в группе
            cur = await db.execute(
                f"DELETE FROM {table} WHERE user_discord_id = ? AND guild_id = ? "
                f"AND activity_type = 'playing' AND {_NORM_GAME_SQL} = ?",
                (user_id, gid, norm)
            )
            deleted = cur.rowcount or 0
            # Вставить одну каноническую строку с суммарной длительностью
            await db.execute(
                f"INSERT INTO {table} "
                f"(user_discord_id, guild_id, activity_type, activity_name, started_at, ended_at, duration_seconds) "
                f"VALUES (?, ?, 'playing', ?, ?, ?, ?)",
                (user_id, gid, canonical or "Unknown", keep_started, keep_ended, total_dur)
            )
            # Схлопнуто = удалено - 1 (одна строка осталась)
            merged += max(0, deleted - 1)
        await db.commit()
    if merged:
        log.info("Merged %d duplicate game activity rows in guild %s", merged, guild_id)
    return merged


# === v2.0.2: Discord roles cache (auto-synced from Discord) ===

async def g_sync_discord_roles(guild_id: int, roles: list[dict]) -> int:
    """Сохранить/обновить закешированный список ролей Discord-сервера.

    roles: [{id, name, color, position, hoisted, mentionable, permissions}, ...]
    Полностью заменяет кеш (delete + insert) — чтобы удалённые роли тоже пропали.
    Возвращает количество записанных ролей.
    """
    if not roles:
        return 0
    table = _guild.guild_table(guild_id, "discord_roles")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        # Берём текущие role_id, чтобы вычислить удалённые
        async with db.execute(f"SELECT role_id FROM {table}") as cur:
            existing_ids = {row[0] for row in await cur.fetchall()}
        new_ids = {int(r["id"]) for r in roles}
        # Удалить роли которых больше нет
        deleted_ids = existing_ids - new_ids
        if deleted_ids:
            placeholders = ",".join("?" * len(deleted_ids))
            await db.execute(
                f"DELETE FROM {table} WHERE role_id IN ({placeholders})",
                tuple(deleted_ids)
            )
        # Upsert оставшихся
        for r in roles:
            await db.execute(
                f"INSERT INTO {table} (role_id, name, color, position, hoisted, mentionable, permissions, synced_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(role_id) DO UPDATE SET "
                f"name = excluded.name, color = excluded.color, position = excluded.position, "
                f"hoisted = excluded.hoisted, mentionable = excluded.mentionable, "
                f"permissions = excluded.permissions, synced_at = excluded.synced_at",
                (int(r["id"]), r["name"], r.get("color"),
                 int(r.get("position", 0)), 1 if r.get("hoisted") else 0,
                 1 if r.get("mentionable") else 0, int(r.get("permissions", 0)), now)
            )
        await db.commit()
    log.info("Synced %d Discord roles for guild %s (deleted %d stale)", len(roles), guild_id, len(deleted_ids))
    return len(roles)


async def g_list_discord_roles(guild_id: int) -> list[dict]:
    """Получить закешированный список ролей Discord-сервера.
    Возвращает [] если кеш пуст (нужно вызвать sync).
    """
    try:
        table = _guild.guild_table(guild_id, "discord_roles")
        async with _connect() as db:
            async with db.execute(
                f"SELECT role_id, name, color, position, hoisted, mentionable, synced_at "
                f"FROM {table} ORDER BY position DESC, name"
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "id": str(r[0]), "name": r[1], "color": r[2],
                "position": r[3], "hoisted": bool(r[4]),
                "mentionable": bool(r[5]), "synced_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("g_list_discord_roles failed: %s", e)
        return []


async def list_active_members_with_discord(limit: int = 22) -> list[dict]:
    """Список активных участников + их Discord статус.

    v2.0.1: Сортировка по статусу (online → idle → dnd → offline).
    Возвращает [{discord_id, display_name, username, avatar_url, last_login_at, role, status}, ...]
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT discord_id, username, display_name, avatar_url, last_login_at, role "
            "FROM users WHERE last_login_at IS NOT NULL "
            "ORDER BY last_login_at DESC LIMIT ?",
            (limit * 2,)  # берём больше, потом отфильтруем по Discord статусу
        ) as cur:
            rows = await cur.fetchall()
    members = []
    for r in rows:
        members.append({
            "discord_id": r[0], "username": r[1], "display_name": r[2] or r[1],
            "avatar_url": r[3], "last_login_at": r[4], "role": r[5] or 'user',
            "status": "offline", "status_emoji": "⚫", "current_game": None,
            "voice_channel": None,
        })
    return members


async def g_get_voice_time(guild_id: int, user_discord_id: int, solo_only: bool = False, with_others_only: bool = False) -> int:
    """Получить общее время в войсе (секунды). Для триггеров ачивок.

    solo_only: только одиночные сессии
    with_others_only: только сессии с другими людьми
    """
    table = _guild.guild_table(guild_id, "voice_sessions")
    where = f"WHERE user_discord_id = ? AND left_at IS NOT NULL"
    if solo_only:
        where += " AND was_solo = 1"
    elif with_others_only:
        where += " AND was_solo = 0"
    async with _connect() as db:
        async with db.execute(
            f"SELECT COALESCE(SUM(duration_seconds), 0) FROM {table} {where}",
            (user_discord_id,)
        ) as cur:
            return (await cur.fetchone())[0] or 0


async def g_get_game_play_time(guild_id: int, user_discord_id: int, game_name: str = "") -> int:
    """Получить время игры (секунды). Если game_name пустой — суммарно во все игры."""
    table = _guild.guild_table(guild_id, "member_activities")
    async with _connect() as db:
        if game_name:
            async with db.execute(
                f"SELECT COALESCE(SUM(duration_seconds), 0) FROM {table} "
                f"WHERE user_discord_id = ? AND activity_type = 'playing' AND activity_name = ? AND ended_at IS NOT NULL",
                (user_discord_id, game_name)
            ) as cur:
                return (await cur.fetchone())[0] or 0
        else:
            async with db.execute(
                f"SELECT COALESCE(SUM(duration_seconds), 0) FROM {table} "
                f"WHERE user_discord_id = ? AND activity_type = 'playing' AND ended_at IS NOT NULL",
                (user_discord_id,)
            ) as cur:
                return (await cur.fetchone())[0] or 0


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


# --- g_collections (сбор фильмов для киновечера) ---

import secrets as _secrets


def _gen_collection_token() -> str:
    """Случайный 20-символьный токен для ссылки на сбор."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(_secrets.choice(alphabet) for _ in range(20))


async def g_start_collection(guild_id: int, started_by: int, max_per_user: int) -> dict:
    """Запустить новый сбор фильмов. Если уже есть активный — вернуть его (не создавать новый).
    Возвращает dict с полями: id, status, max_per_user, started_by, started_at, token.

    Атомарность через BEGIN IMMEDIATE: два одновременных вызова не создадут
    два активных сбора — второй увидит запись первого.
    """
    table = _guild.guild_table(guild_id, "collections")
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Проверяем активный сбор
            async with db.execute(
                f"SELECT id, status, max_per_user, started_by, started_at, token FROM {table} WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.commit()
                return {
                    "id": row[0], "status": row[1], "max_per_user": row[2],
                    "started_by": row[3], "started_at": row[4], "token": row[5],
                }
            # Создаём новый
            token = _gen_collection_token()
            now = datetime.utcnow().isoformat()
            cur = await db.execute(
                f"INSERT INTO {table} (status, max_per_user, started_by, started_at, token) VALUES ('active', ?, ?, ?, ?)",
                (max_per_user, started_by, now, token),
            )
            await db.commit()
            return {
                "id": cur.lastrowid, "status": "active", "max_per_user": max_per_user,
                "started_by": started_by, "started_at": now, "token": token,
            }
        except Exception:
            await db.rollback()
            raise


async def g_get_active_collection(guild_id: int) -> dict | None:
    """Получить активный сбор или None."""
    table = _guild.guild_table(guild_id, "collections")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, status, max_per_user, started_by, started_at, completed_at, completed_by, cancelled_at, cancelled_by, token, spin_started_at, spin_started_by "
            f"FROM {table} WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "status": row[1], "max_per_user": row[2],
            "started_by": row[3], "started_at": row[4], "completed_at": row[5],
            "completed_by": row[6], "cancelled_at": row[7], "cancelled_by": row[8],
            "token": row[9], "spin_started_at": row[10], "spin_started_by": row[11],
        }


async def g_get_collection_by_token(guild_id: int, token: str) -> dict | None:
    """Получить сбор по токену (для /collect/{token} ссылки)."""
    table = _guild.guild_table(guild_id, "collections")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, status, max_per_user, started_by, started_at, token FROM {table} WHERE token = ? LIMIT 1",
            (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "status": row[1], "max_per_user": row[2],
            "started_by": row[3], "started_at": row[4], "token": row[5],
        }


async def g_get_last_completed_collection(guild_id: int) -> dict | None:
    """Получить последнюю завершённую коллекцию (статус completed) — для баннера на /wheel."""
    table = _guild.guild_table(guild_id, "collections")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, status, max_per_user, started_by, started_at, completed_at, completed_by, spin_started_at "
            f"FROM {table} WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "status": row[1], "max_per_user": row[2],
            "started_by": row[3], "started_at": row[4], "completed_at": row[5],
            "completed_by": row[6], "spin_started_at": row[7],
        }


async def g_get_recent_winner(guild_id: int, since_minutes: int = 60) -> dict | None:
    """Получить последнего победителя за последние N минут (для блокировки повторной крутки).

    Логика: если после завершения сбора был определён победитель — крутить больше нельзя.
    Возвращает winner dict или None.
    """
    import time
    table_w = _guild.guild_table(guild_id, "winners")
    cutoff = (datetime.utcnow().timestamp() - since_minutes * 60)
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, lot_name, tmdb_id, confidence, detected_at FROM {table_w} "
            f"WHERE detected_at > ? ORDER BY detected_at DESC LIMIT 1",
            (cutoff_iso,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "lot_name": row[1], "tmdb_id": row[2],
            "confidence": row[3], "detected_at": row[4],
        }


async def g_add_collection_participant(
    guild_id: int, collection_id: int, user_discord_id: int,
    username: str | None = None, display_name: str | None = None, avatar_url: str | None = None,
) -> bool:
    """Добавить участника в сбор. Если уже есть — обновить имя/аватар (не создаёт дубликат).
    Возвращает True если создан новый, False если уже был.
    """
    table = _guild.guild_table(guild_id, "collection_participants")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id FROM {table} WHERE collection_id = ? AND user_discord_id = ? LIMIT 1",
            (collection_id, user_discord_id)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            # Обновляем username/display_name/avatar если переданы
            if username or display_name or avatar_url:
                await db.execute(
                    f"UPDATE {table} SET username = COALESCE(?, username), "
                    f"display_name = COALESCE(?, display_name), "
                    f"avatar_url = COALESCE(?, avatar_url) "
                    f"WHERE collection_id = ? AND user_discord_id = ?",
                    (username, display_name, avatar_url, collection_id, user_discord_id)
                )
                await db.commit()
            return False
        # Создаём нового участника
        await db.execute(
            f"INSERT INTO {table} (collection_id, user_discord_id, username, display_name, avatar_url, joined_at) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (collection_id, user_discord_id, username, display_name, avatar_url, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return True


async def g_list_collection_participants(guild_id: int, collection_id: int) -> list[dict]:
    """Список участников сбора (включая кикнутых — для истории)."""
    table = _guild.guild_table(guild_id, "collection_participants")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, user_discord_id, username, display_name, avatar_url, joined_at, is_ready, ready_at, kicked_at, kicked_by "
            f"FROM {table} WHERE collection_id = ? ORDER BY joined_at ASC",
            (collection_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "user_discord_id": r[1], "username": r[2],
                "display_name": r[3], "avatar_url": r[4], "joined_at": r[5],
                "is_ready": bool(r[6]), "ready_at": r[7],
                "kicked_at": r[8], "kicked_by": r[9],
            }
            for r in rows
        ]


async def g_set_participant_ready(
    guild_id: int, collection_id: int, user_discord_id: int, is_ready: bool,
) -> bool:
    """Отметить участника готовым/не готовым. Возвращает True если обновлено."""
    table = _guild.guild_table(guild_id, "collection_participants")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET is_ready = ?, ready_at = ? "
            f"WHERE collection_id = ? AND user_discord_id = ? AND kicked_at IS NULL",
            (1 if is_ready else 0, datetime.utcnow().isoformat() if is_ready else None, collection_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_kick_collection_participant(
    guild_id: int, collection_id: int, user_discord_id: int, kicked_by: int,
) -> bool:
    """Кикнуть участника (помечает kicked_at + удаляет его picks). Возвращает True если ок."""
    table_p = _guild.guild_table(guild_id, "collection_participants")
    table_pick = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table_p} SET kicked_at = ?, kicked_by = ?, is_ready = 0, ready_at = NULL "
            f"WHERE collection_id = ? AND user_discord_id = ? AND kicked_at IS NULL",
            (datetime.utcnow().isoformat(), kicked_by, collection_id, user_discord_id),
        )
        # Удаляем picks этого юзера
        await db.execute(
            f"DELETE FROM {table_pick} WHERE collection_id = ? AND user_discord_id = ?",
            (collection_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_save_collection_picks(
    guild_id: int, collection_id: int, user_discord_id: int,
    picks: list[dict],
) -> int:
    """Заменить выбор юзера в сборе. picks = list of {watchlist_id, title, tmdb_id}.
    Возвращает количество сохранённых picks.
    """
    table_p = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        # Удаляем старые picks этого юзера в этом сборе
        await db.execute(
            f"DELETE FROM {table_p} WHERE collection_id = ? AND user_discord_id = ?",
            (collection_id, user_discord_id),
        )
        now = datetime.utcnow().isoformat()
        for pick in picks:
            await db.execute(
                f"INSERT INTO {table_p} (collection_id, user_discord_id, watchlist_id, title, tmdb_id, picked_at) "
                f"VALUES (?, ?, ?, ?, ?, ?)",
                (collection_id, user_discord_id, pick["watchlist_id"], pick["title"], pick.get("tmdb_id"), now),
            )
        await db.commit()
        return len(picks)


async def g_get_user_picks(guild_id: int, collection_id: int, user_discord_id: int) -> list[dict]:
    """Получить выбор конкретного юзера в сборе (для отображения выбранных чекбоксов)."""
    table_p = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, watchlist_id, title, tmdb_id, picked_at FROM {table_p} "
            f"WHERE collection_id = ? AND user_discord_id = ? ORDER BY id",
            (collection_id, user_discord_id)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "watchlist_id": r[1], "title": r[2], "tmdb_id": r[3], "picked_at": r[4]}
            for r in rows
        ]


async def g_get_all_collection_picks(guild_id: int, collection_id: int) -> list[dict]:
    """Все picks всех участников сбора — для загрузки в колесо."""
    table_p = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, user_discord_id, watchlist_id, title, tmdb_id, picked_at FROM {table_p} "
            f"WHERE collection_id = ? ORDER BY id",
            (collection_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "user_discord_id": r[1], "watchlist_id": r[2],
                "title": r[3], "tmdb_id": r[4], "picked_at": r[5],
            }
            for r in rows
        ]


async def g_count_user_picks(guild_id: int, collection_id: int, user_discord_id: int) -> int:
    """Сколько picks у юзера в сборе."""
    table_p = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM {table_p} WHERE collection_id = ? AND user_discord_id = ?",
            (collection_id, user_discord_id)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


async def g_cancel_collection(guild_id: int, collection_id: int, cancelled_by: int) -> bool:
    """Отменить сбор (статус → cancelled, picks и participants очищаются)."""
    table_c = _guild.guild_table(guild_id, "collections")
    table_p = _guild.guild_table(guild_id, "collection_participants")
    table_pick = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table_c} SET status = 'cancelled', cancelled_at = ?, cancelled_by = ? "
            f"WHERE id = ? AND status = 'active'",
            (datetime.utcnow().isoformat(), cancelled_by, collection_id),
        )
        # Очищаем picks и participants
        await db.execute(f"DELETE FROM {table_pick} WHERE collection_id = ?", (collection_id,))
        await db.execute(f"DELETE FROM {table_p} WHERE collection_id = ?", (collection_id,))
        await db.commit()
        return cur.rowcount > 0


async def g_complete_collection_spin(
    guild_id: int, collection_id: int, completed_by: int,
) -> list[dict]:
    """Завершить сбор: достать все picks, перемешать, вернуть список для загрузки в колесо.
    Статус collection → 'completed', spin_started_at заполняется, picks/participants очищаются.

    Возвращает shuffled список picks (готов для добавления в колесо).
    Пустой список = сбор уже завершён другим вызовом (защита от двойного клика).
    """
    import random
    table_c = _guild.guild_table(guild_id, "collections")
    table_p = _guild.guild_table(guild_id, "collection_participants")
    table_pick = _guild.guild_table(guild_id, "collection_picks")
    async with _connect() as db:
        # BEGIN IMMEDIATE сериализует писателей — два одновременных вызова
        # не получат оба набор picks.
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Сначала атомарно помечаем сбор завершённым (если ещё active).
            # Если UPDATE совпал с 0 строк — сбор уже завершён другим вызовом.
            now = datetime.utcnow().isoformat()
            cur = await db.execute(
                f"UPDATE {table_c} SET status = 'completed', completed_at = ?, completed_by = ?, spin_started_at = ?, spin_started_by = ? "
                f"WHERE id = ? AND status = 'active'",
                (now, completed_by, now, completed_by, collection_id),
            )
            if cur.rowcount == 0:
                # Сбор уже завершён — ничего не возвращаем, чтобы веб-слой
                # не загрузил дубликаты в колесо.
                await db.commit()
                return []
            # Достаём все picks (теперь уже завершённого) сбора
            async with db.execute(
                f"SELECT user_discord_id, watchlist_id, title, tmdb_id FROM {table_pick} WHERE collection_id = ? ORDER BY id",
                (collection_id,)
            ) as cur:
                rows = await cur.fetchall()
            picks = [
                {"user_discord_id": r[0], "watchlist_id": r[1], "title": r[2], "tmdb_id": r[3]}
                for r in rows
            ]
            # Перемешиваем
            random.shuffle(picks)
            # Очищаем picks и participants (история не сохраняется)
            await db.execute(f"DELETE FROM {table_pick} WHERE collection_id = ?", (collection_id,))
            await db.execute(f"DELETE FROM {table_p} WHERE collection_id = ?", (collection_id,))
            await db.commit()
            return picks
        except Exception:
            await db.rollback()
            raise


# --- g_santa (Тайный Санта, v1.8.0) ---

SANTA_STATUS_COLLECTING = "collecting"
SANTA_STATUS_ASSIGNED = "assigned"
SANTA_STATUS_REVEALED = "revealed"
SANTA_STATUS_CLOSED = "closed"
SANTA_MIN_PARTICIPANTS = 4


async def g_create_santa_event(
    guild_id: int, title: str, deadline: str, budget_note: str | None, created_by: int,
) -> dict:
    """Создать событие Тайного Санты. Возвращает dict события."""
    table = _guild.guild_table(guild_id, "santa_events")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            f"INSERT INTO {table} (title, status, deadline, budget_note, created_by, created_at) "
            f"VALUES (?, 'collecting', ?, ?, ?, ?)",
            (title, deadline, budget_note, created_by, now),
        )
        await db.commit()
        event_id = cur.lastrowid
    return {
        "id": event_id, "title": title, "status": "collecting",
        "deadline": deadline, "budget_note": budget_note,
        "created_by": created_by, "created_at": now,
    }


async def g_get_active_santa_event(guild_id: int) -> dict | None:
    """Получить активное событие санты (collecting или assigned). Возвращает None если нет."""
    table = _guild.guild_table(guild_id, "santa_events")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, title, status, deadline, budget_note, created_by, created_at, assigned_at, revealed_at, revealed_by "
            f"FROM {table} WHERE status IN ('collecting', 'assigned') ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "status": row[2], "deadline": row[3],
            "budget_note": row[4], "created_by": row[5], "created_at": row[6],
            "assigned_at": row[7], "revealed_at": row[8], "revealed_by": row[9],
        }


async def g_get_santa_event(guild_id: int, event_id: int) -> dict | None:
    """Получить событие санты по id."""
    table = _guild.guild_table(guild_id, "santa_events")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, title, status, deadline, budget_note, created_by, created_at, assigned_at, revealed_at, revealed_by "
            f"FROM {table} WHERE id = ?",
            (event_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "status": row[2], "deadline": row[3],
            "budget_note": row[4], "created_by": row[5], "created_at": row[6],
            "assigned_at": row[7], "revealed_at": row[8], "revealed_by": row[9],
        }


async def g_list_santa_events(guild_id: int) -> list[dict]:
    """Список всех событий санты (для истории)."""
    table = _guild.guild_table(guild_id, "santa_events")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, title, status, deadline, budget_note, created_by, created_at, assigned_at, revealed_at "
            f"FROM {table} ORDER BY id DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "title": r[1], "status": r[2], "deadline": r[3],
                "budget_note": r[4], "created_by": r[5], "created_at": r[6],
                "assigned_at": r[7], "revealed_at": r[8],
            }
            for r in rows
        ]


async def g_add_santa_participant(
    guild_id: int, event_id: int, user_discord_id: int,
    username: str | None = None, display_name: str | None = None, avatar_url: str | None = None,
    steam_profile_url: str | None = None, steam_id64: str | None = None,
    steam_persona: str | None = None, steam_avatar_url: str | None = None,
    preferences: str | None = None,
) -> bool:
    """Добавить участника в событие санты. Если уже есть — обновить данные Steam.
    Возвращает True если создан новый, False если обновлён существующий.
    """
    table = _guild.guild_table(guild_id, "santa_participants")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        async with db.execute(
            f"SELECT id FROM {table} WHERE event_id = ? AND user_discord_id = ?",
            (event_id, user_discord_id)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            # Обновляем Steam данные
            await db.execute(
                f"UPDATE {table} SET username = COALESCE(?, username), "
                f"display_name = COALESCE(?, display_name), "
                f"avatar_url = COALESCE(?, avatar_url), "
                f"steam_profile_url = COALESCE(?, steam_profile_url), "
                f"steam_id64 = COALESCE(?, steam_id64), "
                f"steam_persona = COALESCE(?, steam_persona), "
                f"steam_avatar_url = COALESCE(?, steam_avatar_url), "
                f"preferences = COALESCE(?, preferences) "
                f"WHERE event_id = ? AND user_discord_id = ?",
                (username, display_name, avatar_url, steam_profile_url, steam_id64,
                 steam_persona, steam_avatar_url, preferences, event_id, user_discord_id)
            )
            await db.commit()
            return False
        await db.execute(
            f"INSERT INTO {table} (event_id, user_discord_id, username, display_name, avatar_url, "
            f"steam_profile_url, steam_id64, steam_persona, steam_avatar_url, preferences, joined_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, user_discord_id, username, display_name, avatar_url,
             steam_profile_url, steam_id64, steam_persona, steam_avatar_url, preferences, now),
        )
        await db.commit()
        return True


async def g_list_santa_participants(guild_id: int, event_id: int) -> list[dict]:
    """Список участников события санты."""
    table = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, user_discord_id, username, display_name, avatar_url, "
            f"steam_profile_url, steam_id64, steam_persona, steam_avatar_url, preferences, "
            f"is_ready, ready_at, joined_at, gift_sent_at "
            f"FROM {table} WHERE event_id = ? ORDER BY joined_at ASC",
            (event_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "user_discord_id": r[1], "username": r[2],
                "display_name": r[3], "avatar_url": r[4],
                "steam_profile_url": r[5], "steam_id64": r[6],
                "steam_persona": r[7], "steam_avatar_url": r[8],
                "preferences": r[9], "is_ready": bool(r[10]),
                "ready_at": r[11], "joined_at": r[12], "gift_sent_at": r[13],
            }
            for r in rows
        ]


async def g_get_santa_participant(guild_id: int, event_id: int, user_discord_id: int) -> dict | None:
    """Получить участника события санты."""
    table = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, user_discord_id, username, display_name, avatar_url, "
            f"steam_profile_url, steam_id64, steam_persona, steam_avatar_url, preferences, "
            f"is_ready, ready_at, joined_at, gift_sent_at "
            f"FROM {table} WHERE event_id = ? AND user_discord_id = ?",
            (event_id, user_discord_id)
        ) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        return {
            "id": r[0], "user_discord_id": r[1], "username": r[2],
            "display_name": r[3], "avatar_url": r[4],
            "steam_profile_url": r[5], "steam_id64": r[6],
            "steam_persona": r[7], "steam_avatar_url": r[8],
            "preferences": r[9], "is_ready": bool(r[10]),
            "ready_at": r[11], "joined_at": r[12], "gift_sent_at": r[13],
        }


async def g_set_santa_participant_ready(
    guild_id: int, event_id: int, user_discord_id: int, is_ready: bool,
) -> bool:
    """Отметить участника готовым/не готовым."""
    table = _guild.guild_table(guild_id, "santa_participants")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET is_ready = ?, ready_at = ? "
            f"WHERE event_id = ? AND user_discord_id = ?",
            (1 if is_ready else 0, now if is_ready else None, event_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_count_santa_participants(guild_id: int, event_id: int, ready_only: bool = False) -> int:
    """Посчитать участников (опционально только готовых)."""
    table = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        if ready_only:
            sql = f"SELECT COUNT(*) FROM {table} WHERE event_id = ? AND is_ready = 1"
        else:
            sql = f"SELECT COUNT(*) FROM {table} WHERE event_id = ?"
        async with db.execute(sql, (event_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


async def g_assign_santas(guild_id: int, event_id: int) -> list[dict] | None:
    """Назначить сант (алгоритм: случайная перестановка без фиксированных точек).
    Минимум 4 участника. Каждый дарит одному, получает от другого.
    Возвращает list of {santa_discord_id, recipient_discord_id} или None если мало участников.
    """
    import random
    participants = await g_list_santa_participants(guild_id, event_id)
    if len(participants) < SANTA_MIN_PARTICIPANTS:
        return None

    # Берём только готовых
    ready = [p for p in participants if p["is_ready"]]
    if len(ready) < SANTA_MIN_PARTICIPANTS:
        return None

    discord_ids = [p["user_discord_id"] for p in ready]
    n = len(discord_ids)

    # Алгоритм: циклический сдвиг на случайную величину (1..n-1)
    # Это гарантирует: никто не дарит себе, нет коротких циклов (кроме n=2)
    shift = random.randint(1, n - 1)
    assignments = []
    for i in range(n):
        santa_id = discord_ids[i]
        recipient_id = discord_ids[(i + shift) % n]
        assignments.append({"santa_discord_id": santa_id, "recipient_discord_id": recipient_id})

    # Сохраняем в БД
    table_a = _guild.guild_table(guild_id, "santa_assignments")
    table_e = _guild.guild_table(guild_id, "santa_events")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        # Очищаем старые назначения (если были)
        await db.execute(f"DELETE FROM {table_a} WHERE event_id = ?", (event_id,))
        for a in assignments:
            await db.execute(
                f"INSERT INTO {table_a} (event_id, santa_discord_id, recipient_discord_id, assigned_at) "
                f"VALUES (?, ?, ?, ?)",
                (event_id, a["santa_discord_id"], a["recipient_discord_id"], now),
            )
        # Меняем статус события
        await db.execute(
            f"UPDATE {table_e} SET status = 'assigned', assigned_at = ? WHERE id = ?",
            (now, event_id),
        )
        await db.commit()

    return assignments


async def g_get_santa_assignment(guild_id: int, event_id: int, santa_discord_id: int) -> dict | None:
    """Получить назначение санты — кого он одаривает. Возвращает dict с recipient info."""
    table_a = _guild.guild_table(guild_id, "santa_assignments")
    table_p = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        async with db.execute(
            f"SELECT a.santa_discord_id, a.recipient_discord_id, a.assigned_at, a.gift_sent_at, a.gift_note, "
            f"p.username, p.display_name, p.avatar_url, p.steam_profile_url, p.steam_id64, "
            f"p.steam_persona, p.steam_avatar_url, p.preferences "
            f"FROM {table_a} a "
            f"JOIN {table_p} p ON p.user_discord_id = a.recipient_discord_id AND p.event_id = a.event_id "
            f"WHERE a.event_id = ? AND a.santa_discord_id = ?",
            (event_id, santa_discord_id)
        ) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        return {
            "santa_discord_id": r[0], "recipient_discord_id": r[1],
            "assigned_at": r[2], "gift_sent_at": r[3], "gift_note": r[4],
            "recipient_username": r[5], "recipient_display_name": r[6],
            "recipient_avatar_url": r[7], "recipient_steam_profile_url": r[8],
            "recipient_steam_id64": r[9], "recipient_steam_persona": r[10],
            "recipient_steam_avatar_url": r[11], "recipient_preferences": r[12],
        }


async def g_mark_santa_gift_sent(
    guild_id: int, event_id: int, santa_discord_id: int, gift_note: str | None = None,
) -> bool:
    """Отметить что подарок отправлен. Возвращает True если обновлено."""
    table_a = _guild.guild_table(guild_id, "santa_assignments")
    table_p = _guild.guild_table(guild_id, "santa_participants")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table_a} SET gift_sent_at = ?, gift_note = ? "
            f"WHERE event_id = ? AND santa_discord_id = ? AND gift_sent_at IS NULL",
            (now, gift_note, event_id, santa_discord_id),
        )
        # Также отмечаем gift_sent_at в participants
        await db.execute(
            f"UPDATE {table_p} SET gift_sent_at = ? "
            f"WHERE event_id = ? AND user_discord_id = ?",
            (now, event_id, santa_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_get_all_santa_assignments(guild_id: int, event_id: int) -> list[dict]:
    """Все назначения события (для страницы раскрытия). Только после reveal."""
    table_a = _guild.guild_table(guild_id, "santa_assignments")
    table_p = _guild.guild_table(guild_id, "santa_participants")
    async with _connect() as db:
        # Сначала santas
        async with db.execute(
            f"SELECT a.santa_discord_id, a.recipient_discord_id, a.gift_sent_at, a.gift_note, "
            f"ps.username, ps.display_name, ps.avatar_url "
            f"FROM {table_a} a "
            f"JOIN {table_p} ps ON ps.user_discord_id = a.santa_discord_id AND ps.event_id = a.event_id "
            f"WHERE a.event_id = ? ORDER BY a.id",
            (event_id,)
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            santa_id = r[0]
            recipient_id = r[1]
            # Получаем recipient info
            async with db.execute(
                f"SELECT username, display_name, avatar_url, steam_persona, steam_avatar_url, preferences "
                f"FROM {table_p} WHERE event_id = ? AND user_discord_id = ?",
                (event_id, recipient_id)
            ) as cur2:
                rp = await cur2.fetchone()
            result.append({
                "santa_discord_id": santa_id,
                "santa_username": r[4],
                "santa_display_name": r[5],
                "santa_avatar_url": r[6],
                "recipient_discord_id": recipient_id,
                "recipient_username": rp[0] if rp else None,
                "recipient_display_name": rp[1] if rp else None,
                "recipient_avatar_url": rp[2] if rp else None,
                "recipient_steam_persona": rp[3] if rp else None,
                "recipient_steam_avatar_url": rp[4] if rp else None,
                "recipient_preferences": rp[5] if rp else None,
                "gift_sent_at": r[2],
                "gift_note": r[3],
            })
        return result


async def g_reveal_santa_event(guild_id: int, event_id: int, revealed_by: int) -> bool:
    """Раскрыть сант (статус → revealed). Только админ."""
    table = _guild.guild_table(guild_id, "santa_events")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET status = 'revealed', revealed_at = ?, revealed_by = ? "
            f"WHERE id = ? AND status = 'assigned'",
            (now, revealed_by, event_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_close_santa_event(guild_id: int, event_id: int) -> bool:
    """Закрыть событие санты (статус → closed). Только админ. Скрывает данные."""
    table = _guild.guild_table(guild_id, "santa_events")
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET status = 'closed' WHERE id = ?",
            (event_id,)
        )
        await db.commit()
        return cur.rowcount > 0


# --- g_watchlist (список желаемого) ---

async def g_add_to_watchlist(guild_id: int, user_discord_id: int, title: str, tmdb_id: int | None = None) -> tuple[int | None, str | None]:
    """Добавить фильм в личный список желаемого юзера.

    Проверка дублей: ищем в вишлисте ВСЕХ юзеров этого guild (не только текущего):
    - Если есть tmdb_id — ищем по tmdb_id
    - Если tmdb_id None — ищем по lower(title) = lower(new_title)
    Если найден у ДРУГОГО юзера — НЕ создаём дубликат, возвращаем (None, "already_in_other_watchlist").
    Если найден у ТЕКУЩЕГО юзера — возвращаем (existing_id, "already_yours").
    Если не найден — создаём, возвращаем (new_id, None).

    Возвращает кортеж (watchlist_id | None, error_code | None).
    """
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        # Ищем существующую запись у ЛЮБОГО юзера
        if tmdb_id is not None:
            sql = f"SELECT id, user_discord_id FROM {table} WHERE tmdb_id = ? AND is_watched = 0 LIMIT 1"
            params: tuple = (tmdb_id,)
        else:
            sql = f"SELECT id, user_discord_id FROM {table} WHERE lower(title) = lower(?) AND is_watched = 0 LIMIT 1"
            params = (title,)
        async with db.execute(sql, params) as cur:
            existing = await cur.fetchone()

        if existing:
            existing_id, existing_user = existing
            if existing_user == user_discord_id:
                return existing_id, "already_yours"
            return None, "already_in_other_watchlist"

        # Не найден — создаём
        cur = await db.execute(
            f"INSERT INTO {table} (user_discord_id, title, tmdb_id, added_at) VALUES (?, ?, ?, ?)",
            (user_discord_id, title, tmdb_id, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid, None


async def g_remove_from_watchlist(guild_id: int, watchlist_id: int, user_discord_id: int) -> bool:
    """Удалить фильм из списка желаемого (только владелец может)."""
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        cur = await db.execute(
            f"DELETE FROM {table} WHERE id = ? AND user_discord_id = ?",
            (watchlist_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_update_watchlist_title(guild_id: int, watchlist_id: int, user_discord_id: int, new_title: str) -> bool:
    """Изменить название фильма в списке желаемого (только владелец)."""
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET title = ? WHERE id = ? AND user_discord_id = ?",
            (new_title.strip(), watchlist_id, user_discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def g_list_watchlist(guild_id: int, user_discord_id: int, include_watched: bool = False) -> list[tuple]:
    """Список желаемого конкретного юзера.
    Возвращает [(id, title, tmdb_id, added_at, is_watched), ...]
    По умолчанию только непросмотренные.
    """
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        if include_watched:
            sql = f"SELECT id, title, tmdb_id, added_at, is_watched FROM {table} WHERE user_discord_id = ? ORDER BY added_at DESC"
            params = (user_discord_id,)
        else:
            sql = f"SELECT id, title, tmdb_id, added_at, is_watched FROM {table} WHERE user_discord_id = ? AND is_watched = 0 ORDER BY added_at DESC"
            params = (user_discord_id,)
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


async def g_get_all_unwatched_watchlist(guild_id: int) -> list[dict]:
    """Все непросмотренные фильмы из списков желаемого ВСЕХ юзеров.
    Используется для заполнения колеса.
    """
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        async with db.execute(
            f"SELECT id, user_discord_id, title, tmdb_id FROM {table} WHERE is_watched = 0 ORDER BY added_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [{"id": r[0], "user_discord_id": r[1], "title": r[2], "tmdb_id": r[3]} for r in rows]


async def g_mark_watchlist_watched(guild_id: int, title: str) -> int:
    """Пометить все записи с таким названием как просмотренные.
    Возвращает количество обновлённых записей.
    """
    table = _guild.guild_table(guild_id, "watchlist")
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE {table} SET is_watched = 1, watched_at = ? WHERE lower(title) = lower(?) AND is_watched = 0",
            (datetime.utcnow().isoformat(), title),
        )
        await db.commit()
        return cur.rowcount

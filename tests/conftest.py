"""Общие фикстуры для интеграционных тестов dibilkinbot (Фаза 0).

ВАЖНО — env-переменные выставляются на УРОВНЕ МОДУЛЯ, до импорта config/web/db:
config.py читает env при импорте (Settings.from_env) и падает без APP_SECRET,
поэтому conftest обязан отработать раньше первого `import web`.

База — синтетическая (db.init_db() при старте TestClient), снапшот продакшн-БД
не нужен. Тесты запускаются из корня репо:

    python -m pytest tests/test_wheel_integration.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Корень репо в sys.path — чтобы `import web` / `import db` работали
# независимо от того, откуда запущен pytest.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# === Тестовая гильдия ===
# Реальный 18-значный Discord ID (> 2^53) — заодно регрессия на потерю
# точности guild_id в WS-канале (см. AUDIT_guild_id_precision.md):
# query-параметр приходит строкой, сервер парсит int() без потери точности.
TEST_GUILD_ID = 1515661305011376209

# Discord ID тестовой «Матки». Не пересекается с TEST_GUILD_ID.
TEST_ADMIN_DISCORD_ID = 1001

_TMP_DIR = tempfile.mkdtemp(prefix="dibilkinbot_tests_")

# === env ДО импорта config/web/db ===
from cryptography.fernet import Fernet  # noqa: E402

# Fernet-ключ генерируется заново на каждый прогон (не захардкожен).
os.environ["APP_SECRET"] = Fernet.generate_key().decode()
os.environ["DATABASE_PATH"] = str(Path(_TMP_DIR) / "bot.db")
# env-admin не назначаем: роль-логика в этих тестах не задействована,
# а session-payload создаётся напрямую с is_superuser=True.
os.environ["ADMIN_DISCORD_ID"] = ""
# Внешние API отключаем — тесты не должны ходить в сеть.
os.environ.setdefault("DISCORD_TOKEN", "")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("KINOPOISK_TOKEN", "")
os.environ.setdefault("STEAM_API_KEY", "")

import pytest  # noqa: E402


def run_async(coro):
    """Выполнить корутину в отдельном event loop.

    Безопасно для db/guild: каждый вызов открывает собственное
    aiosqlite-соединение (см. db._connect / guild._connect),
    состояние между loop'ами не шарится.
    """
    return asyncio.run(coro)


async def _purge_guild_wheel_data():
    """Очистить wheel-данные тестовой гильдии между тестами.

    Прямой SQL: DELETE из wheel_items / winners / ratings.
    Остальные guild-таблицы тесты колеса не трогают.
    """
    import aiosqlite
    from config import settings
    from guild import guild_table

    async with aiosqlite.connect(settings.database_path) as conn:
        for base in ("wheel_items", "winners", "ratings"):
            table = guild_table(TEST_GUILD_ID, base)
            await conn.execute(f"DELETE FROM {table}")
        await conn.commit()


def _reset_wheel_module_state():
    """Сброс module-level состояния колеса и WS-пула.

    Недождавшиеся фоновые задачи _delayed_complete_spin после
    _active_spins.clear() тихо выходят: повторный lookup spin_state
    возвращает None → ранний return (см. wheel_routes.py:496-498).
    """
    import wheel_routes
    import ws_manager

    wheel_routes._wheel_spin_locks.clear()
    wheel_routes._active_spins.clear()
    ws_manager._guild_connections.clear()
    ws_manager._ws_to_guild.clear()


@pytest.fixture(scope="session")
def client():
    """ЕДИНЫЙ TestClient на всю сессию (один event loop на все тесты).

    Почему session-scope (главный урок Фазы 0):
    _delayed_complete_spin — фоновая задача event loop'а TestClient.
    Если открывать `with TestClient(...)` на каждый тест, loop умирает
    вместе с контекстом фикстуры, недождавшиеся задачи рвутся на середине,
    а занятые _wheel_spin_locks / _active_spins достаются следующему тесту
    → ложные 409 «Колесо уже крутится» при прогоне всего файла (при этом
    тесты по отдельности зелёные).

    Жизненный цикл:
    1. `with TestClient(web.app)` → startup event → db.init_db() (синтетическая БД).
    2. Регистриуем и апрувим тестовую гильдию, создаём guild-таблицы.
    3. Сессия создаётся напрямую через deps.create_session (обход /login —
       см. HANDOFF_wheel_audit.md, раздел 4).
    """
    import web
    from fastapi.testclient import TestClient

    with TestClient(web.app) as c:
        import guild as guild_mod
        import db as db_mod

        run_async(guild_mod.upsert_guild(TEST_GUILD_ID, "Test Guild", auto_approve=True))
        run_async(guild_mod.init_guild_tables(TEST_GUILD_ID))

        # v3.10.0 (B3): создать тестового юзера в БД с role='superuser' —
        # иначе _refresh_role_from_db перечитает role='user' (юзера нет в БД)
        # и все require_superuser тесты получат 403.
        run_async(db_mod.upsert_user(TEST_ADMIN_DISCORD_ID, username="tester", display_name="Tester"))
        run_async(db_mod.set_user_role(TEST_ADMIN_DISCORD_ID, "superuser"))

        import deps

        token = deps.create_session({
            "discord_id": TEST_ADMIN_DISCORD_ID,
            "username": "tester",
            "display_name": "Tester",
            "is_admin": True,
            "is_superuser": True,
            "role": "superuser",
            "current_guild_id": TEST_GUILD_ID,
        })
        c.cookies.set("session", token)
        yield c


@pytest.fixture(autouse=True)
def _isolate_wheel_state(client):
    """Изоляция тестов: чистое колесо до и после каждого теста.

    Сбрасывает module-level состояние (locks, active spins, WS-пул)
    и guild-таблицы (wheel_items, winners, ratings).
    """
    _reset_wheel_module_state()
    run_async(_purge_guild_wheel_data())
    yield
    _reset_wheel_module_state()
    run_async(_purge_guild_wheel_data())

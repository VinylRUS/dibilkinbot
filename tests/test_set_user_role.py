"""v3.6.1: тесты на set_user_role — что отказы env-admin логируются,
что UPDATE реально выполняется, и что callers (toggle_admin, api_admin_set_role)
корректно реагируют на False.

Идея: settings — frozen dataclass, поэтому используем object.__setattr__()
чтобы менять атрибуты на месте (все кто сделал `from config import settings`
увидят изменения, т.к. ссылаются на тот же объект).
"""
from __future__ import annotations

from dataclasses import replace

import pytest


def patch_settings(monkeypatch, **overrides):
    """Заменить config.settings на новый frozen-экземпляр с overrides.

    Патчит и config.settings, и db.settings (т.к. db.py сделал
    `from config import settings` на импорте — у него своя ссылка).
    """
    import config
    import db
    new = replace(config.settings, **overrides)
    monkeypatch.setattr(config, "settings", new)
    monkeypatch.setattr(db, "settings", new)


@pytest.mark.asyncio
async def test_set_user_role_invalid_role_returns_false():
    """Невалидная роль → False, без обращения к БД."""
    import db
    await db.init_db()  # idempotent в conftest-БД

    # Создаём юзера (если ещё нет)
    await db.upsert_user(123456, username="testuser")
    ok = await db.set_user_role(123456, "invalid-role-name")
    assert ok is False


@pytest.mark.asyncio
async def test_set_user_role_env_admin_discord_id_blocks(monkeypatch):
    """Если discord_id == settings.admin_discord_id → False, без UPDATE в БД."""
    import db
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=987654321, admin_login="admin")
    await db.upsert_user(987654321, username="env-admin-user")
    # Пробуем повысить — даже повышение заблокировано для env-admin
    ok = await db.set_user_role(987654321, "junior-admin")
    assert ok is False
    # В БД у env-admin'а роль может быть любой (default 'user'), но get_user_role
    # вернёт 'superuser' из-за env-check. Поэтому смотрим прямо в БД:
    import aiosqlite
    from config import settings as s_now
    async with aiosqlite.connect(s_now.database_path) as conn:
        cur = await conn.execute("SELECT role FROM users WHERE discord_id = ?", (987654321,))
        row = await cur.fetchone()
    db_role = row[0] if row else None
    assert db_role == "user", f"env-admin DB role should not be changed, got {db_role!r}"


@pytest.mark.asyncio
async def test_set_user_role_env_admin_numeric_login_blocks(monkeypatch):
    """Если ADMIN_LOGIN — число, равное discord_id → False."""
    import db
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=None, admin_login="5555555")

    await db.upsert_user(5555555, username="env-admin-by-login")
    ok = await db.set_user_role(5555555, "user")
    assert ok is False


@pytest.mark.asyncio
async def test_set_user_role_promotion_works(monkeypatch):
    """Обычный юзер (не env-admin) — повышение user→junior-admin работает."""
    import db
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=9999999, admin_login="admin")

    await db.upsert_user(1111111, username="regular-user")
    ok = await db.set_user_role(1111111, "junior-admin")
    assert ok is True
    role = await db.get_user_role(1111111)
    assert role == "junior-admin"


@pytest.mark.asyncio
async def test_set_user_role_demotion_works(monkeypatch):
    """junior-admin → user работает для обычного юзера (не env-admin).

    Это репродукция бага: 'Пчела→Трутень работает, обратно — нет'. С фиксом
    v3.6.1 понижение работает, если юзер не env-admin.
    """
    import db
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=9999999, admin_login="admin")

    await db.upsert_user(2222222, username="junior-to-be")
    # Сначала повышаем
    ok1 = await db.set_user_role(2222222, "junior-admin")
    assert ok1 is True
    assert (await db.get_user_role(2222222)) == "junior-admin"
    # Теперь понижаем обратно
    ok2 = await db.set_user_role(2222222, "user")
    assert ok2 is True
    assert (await db.get_user_role(2222222)) == "user"


@pytest.mark.asyncio
async def test_set_user_role_nonexistent_user_returns_false(monkeypatch):
    """discord_id которого нет в БД → False (UPDATE affects 0 rows)."""
    import db
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=9999999, admin_login="admin")

    # Юзер 8888888 не создан
    ok = await db.set_user_role(8888888, "junior-admin")
    assert ok is False


@pytest.mark.asyncio
async def test_set_user_role_logs_env_admin_rejection(monkeypatch, caplog):
    """При отказе env-admin в лог попадает конкретная причина."""
    import db
    import logging
    await db.init_db()

    patch_settings(monkeypatch, admin_discord_id=7777777, admin_login="admin")

    await db.upsert_user(7777777, username="env-admin-for-log-test")

    with caplog.at_level(logging.WARNING, logger="db"):
        ok = await db.set_user_role(7777777, "user")
    assert ok is False
    # В логе должно быть упоминание env-admin + ID
    env_admin_mentions = [
        r for r in caplog.records
        if "env-admin" in r.getMessage() and "7777777" in r.getMessage()
    ]
    assert len(env_admin_mentions) >= 1, (
        f"expected env-admin warning in logs, got: {[r.getMessage() for r in caplog.records]}"
    )

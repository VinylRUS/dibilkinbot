"""Per-guild config ChaosLedger + link table Discord↔Minecraft UUID.

Config хранится в per-guild settings (`guild_{id}_settings`):
  - `cl_enabled`  — "0"/"1" (по умолчанию "0")
  - `cl_api_url`  — http://host:8775 (без trailing slash)
  - `cl_api_key`  — шифруется через crypto.encrypt (Fernet), is_secret=True

Link table `guild_{id}_cl_links` — одна запись на Discord-юзера:
  discord_id INTEGER PRIMARY KEY,
  mc_uuid    TEXT NOT NULL,        -- canonical with dashes
  mc_name    TEXT NOT NULL,
  linked_at  INTEGER NOT NULL

UUID берётся через Mojang API (api.mojang.com) по нику Minecraft.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

import crypto
import db

log = logging.getLogger(__name__)

# --- config keys -------------------------------------------------------------

CFG_ENABLED = "cl_enabled"
CFG_API_URL = "cl_api_url"
CFG_API_KEY = "cl_api_key"  # шифрованный

MOJANG_API = "https://api.mojang.com/users/profiles/minecraft/{nick}"
MOJANG_TIMEOUT = 8.0


# --- config ------------------------------------------------------------------


async def get_cl_config(guild_id: int) -> dict[str, Any]:
    """Возвращает config ChaosLedger для гильдии. api_key расшифровывается.

    Если ничего не настроено — возвращает дефолт (enabled=False, пустые url/key).
    """
    enabled_raw = await db.g_get_setting(guild_id, CFG_ENABLED)
    api_url = await db.g_get_setting(guild_id, CFG_API_URL)
    api_key_enc = await db.g_get_setting(guild_id, CFG_API_KEY)
    api_key = crypto.decrypt(api_key_enc) if api_key_enc else ""
    return {
        "enabled": enabled_raw == "1",
        "api_url": api_url or "",
        "api_key": api_key or "",
    }


async def set_cl_config(
    guild_id: int,
    *,
    enabled: bool | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Обновляет config. None-поля не трогаются. api_key шифруется.

    api_key="" (пустая строка) — осмысленное значение (open mode мода),
    поэтому None = «не менять», "" = «явно поставить пустой».
    """
    if enabled is not None:
        await db.g_set_setting(guild_id, CFG_ENABLED, "1" if enabled else "0")
    if api_url is not None:
        # нормализуем: убираем trailing slash
        url = (api_url or "").strip().rstrip("/")
        await db.g_set_setting(guild_id, CFG_API_URL, url)
    if api_key is not None:
        key = (api_key or "").strip()
        enc = crypto.encrypt(key) if key else None
        await db.g_set_setting(guild_id, CFG_API_KEY, enc, is_secret=bool(enc))
    return await get_cl_config(guild_id)


# --- Mojang UUID resolve -----------------------------------------------------


def _canonical_uuid(raw: str) -> str:
    """Mojang отдаёт UUID без дефисов (32 hex). Конвертируем в canonical 8-4-4-4-12."""
    r = raw.replace("-", "").lower()
    if len(r) != 32 or any(c not in "0123456789abcdef" for c in r):
        raise ValueError(f"bad mojang uuid: {raw!r}")
    return f"{r[:8]}-{r[8:12]}-{r[12:16]}-{r[16:20]}-{r[20:]}"


async def resolve_mc_uuid(nick: str) -> tuple[str, str] | None:
    """Resolve Minecraft username → (canonical_uuid, name) через Mojang API.

    Returns None если игрок не найден (204/404). Raise на сетевой ошибке /
    rate limit (429) / таймауте.
    """
    nick = (nick or "").strip()
    if not nick or len(nick) > 16:
        return None
    url = MOJANG_API.format(nick=nick)
    timeout = aiohttp.ClientTimeout(total=MOJANG_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, headers={"Accept": "application/json"}) as resp:
                if resp.status in (204, 404):
                    return None
                if resp.status == 429:
                    raise RuntimeError("mojang rate limit — попробуйте через минуту")
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"mojang {resp.status}: {text[:120]}")
                body = await resp.json()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"mojang request failed: {e}") from e
    raw_id = body.get("id")
    name = body.get("name") or nick
    if not raw_id:
        return None
    return _canonical_uuid(raw_id), name


# --- link table --------------------------------------------------------------


def _links_table(guild_id: int) -> str:
    return db._guild.guild_table(guild_id, "cl_links")


async def link_player(guild_id: int, discord_id: int, mc_uuid: str, mc_name: str) -> None:
    """Привязать Discord ID к Minecraft UUID (upsert)."""
    table = _links_table(guild_id)
    async with db._connect() as conn:
        await conn.execute(
            f"""INSERT INTO {table} (discord_id, mc_uuid, mc_name, linked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    mc_uuid = excluded.mc_uuid,
                    mc_name = excluded.mc_name,
                    linked_at = excluded.linked_at""",
            (int(discord_id), mc_uuid, mc_name, int(time.time())),
        )
        await conn.commit()


async def unlink_player(guild_id: int, discord_id: int) -> bool:
    """Отвязать Discord ID. Returns True если запись была."""
    table = _links_table(guild_id)
    async with db._connect() as conn:
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE discord_id = ?", (int(discord_id),)
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_link(guild_id: int, discord_id: int) -> dict[str, Any] | None:
    """Возвращает {discord_id, mc_uuid, mc_name, linked_at} или None."""
    table = _links_table(guild_id)
    async with db._connect() as conn:
        async with conn.execute(
            f"""SELECT discord_id, mc_uuid, mc_name, linked_at
                FROM {table} WHERE discord_id = ?""",
            (int(discord_id),),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "discord_id": row[0],
        "mc_uuid": row[1],
        "mc_name": row[2],
        "linked_at": row[3],
    }


async def get_link_by_uuid(guild_id: int, mc_uuid: str) -> dict[str, Any] | None:
    """Обратный lookup: MC UUID → Discord ID. Нужно для webhook-уведомлений."""
    table = _links_table(guild_id)
    async with db._connect() as conn:
        async with conn.execute(
            f"""SELECT discord_id, mc_uuid, mc_name, linked_at
                FROM {table} WHERE mc_uuid = ?""",
            (mc_uuid,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "discord_id": row[0],
        "mc_uuid": row[1],
        "mc_name": row[2],
        "linked_at": row[3],
    }


async def list_links(guild_id: int) -> list[dict[str, Any]]:
    """Все привязки гильдии (для админ-таблицы в /ledger)."""
    table = _links_table(guild_id)
    async with db._connect() as conn:
        async with conn.execute(
            f"""SELECT discord_id, mc_uuid, mc_name, linked_at
                FROM {table} ORDER BY linked_at DESC"""
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "discord_id": r[0],
            "mc_uuid": r[1],
            "mc_name": r[2],
            "linked_at": r[3],
        }
        for r in rows
    ]

"""Village Quest Remake (Minecraft mod) — DB layer for the bot/panel integration.

v3.1: integrates the bot with the Village Quest mod via REST API. See
`rest_api.md` in the mod repo for the contract.

This module owns:
  - per-guild config (transport mode, api_url, api_token, gateway_token, server_id)
  - events journal (events received from mod, persisted for polling/push)
  - commands queue (commands waiting for transport B to send to mod)
  - links cache (discord_id ↔ mc_uuid, populated from mod's link events / claim endpoint)

Config is stored in the existing per-guild `settings` table (key-value with
is_secret flag) with key prefix `vq_`. Tokens are encrypted via crypto module
to match the existing pattern used for discord_token / telegram_token.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

import crypto
import db
from db import _connect, _guild

log = logging.getLogger("vq_data")

# === Config keys in per-guild settings table ===
CONFIG_KEY_TRANSPORT = "vq_transport"          # "A" or "B"
CONFIG_KEY_API_URL = "vq_api_url"              # Transport A: e.g. http://1.2.3.4:8790
CONFIG_KEY_API_TOKEN_ENC = "vq_api_token"      # Transport A: bearer (encrypted)
CONFIG_KEY_GATEWAY_TOKEN_ENC = "vq_gateway_token"  # Transport B: shared secret (encrypted)
CONFIG_KEY_SERVER_ID = "vq_server_id"          # Transport B: server label known to mod
CONFIG_KEY_LAST_EVENT_ID = "vq_last_event_id"  # cursor: last event_id processed
CONFIG_KEY_LAST_COMMAND_ID = "vq_last_command_id"  # cursor: last command_id sent to mod (transport B)
CONFIG_KEY_PANEL_URL = "vq_panel_url"          # informational only — what mod should know

# v3.1.1: per mod spec rest_api.md §2, server_id must match [a-z0-9._-]{1,32}.
# Mod normalizes to lowercase on its side, so we MUST do the same to match
# (case-sensitive comparison would fail otherwise → 403 unknown server_id).
SERVER_ID_RE = re.compile(r"^[a-z0-9._-]{1,32}$")


def _normalize_server_id(server_id: str | None) -> str:
    """Lowercase + strip server_id, default to 'main' if empty.
    Does NOT validate the regex — caller should do that if needed."""
    sid = (server_id or "").strip().lower()
    return sid or "main"


def _validate_server_id(server_id: str) -> None:
    """Raise ValueError if server_id doesn't match mod spec [a-z0-9._-]{1,32}."""
    if not SERVER_ID_RE.match(server_id):
        raise ValueError(
            f"server_id must match [a-z0-9._-] (1-32 chars), got {server_id!r}"
        )



# === Config getters / setters ===

async def get_transport(guild_id: int) -> str:
    """'A' (HTTP client to mod) or 'B' (gateway endpoints for agent mode).
    Default: 'B' (matches exarathon case)."""
    val = await db.g_get_setting(guild_id, CONFIG_KEY_TRANSPORT)
    return val or "B"


async def set_transport(guild_id: int, transport: str) -> None:
    if transport not in ("A", "B"):
        raise ValueError(f"transport must be 'A' or 'B', got {transport!r}")
    await db.g_set_setting(guild_id, CONFIG_KEY_TRANSPORT, transport, is_secret=False)


async def get_api_url(guild_id: int) -> str:
    """Transport A: base URL of the mod's HTTP server (e.g. http://1.2.3.4:8790)."""
    return (await db.g_get_setting(guild_id, CONFIG_KEY_API_URL) or "").strip()


async def set_api_url(guild_id: int, url: str) -> None:
    await db.g_set_setting(guild_id, CONFIG_KEY_API_URL, (url or "").strip(), is_secret=False)


async def get_api_token(guild_id: int) -> str:
    """Transport A: bearer token (decrypted). Empty string if not set."""
    raw = await db.g_get_setting(guild_id, CONFIG_KEY_API_TOKEN_ENC)
    if not raw:
        return ""
    try:
        return crypto.decrypt(raw)
    except Exception as e:
        log.warning("Failed to decrypt vq_api_token for guild %s: %s", guild_id, e)
        return ""


async def set_api_token(guild_id: int, token: str) -> None:
    """Store api_token (encrypted). Empty string clears it."""
    token = (token or "").strip()
    if not token:
        await db.g_delete_setting(guild_id, CONFIG_KEY_API_TOKEN_ENC)
        return
    enc = crypto.encrypt(token)
    await db.g_set_setting(guild_id, CONFIG_KEY_API_TOKEN_ENC, enc, is_secret=True)


async def get_gateway_token(guild_id: int) -> str:
    """Transport B: shared secret between mod and panel (decrypted)."""
    raw = await db.g_get_setting(guild_id, CONFIG_KEY_GATEWAY_TOKEN_ENC)
    if not raw:
        return ""
    try:
        return crypto.decrypt(raw)
    except Exception as e:
        log.warning("Failed to decrypt vq_gateway_token for guild %s: %s", guild_id, e)
        return ""


async def set_gateway_token(guild_id: int, token: str) -> None:
    token = (token or "").strip()
    if not token:
        await db.g_delete_setting(guild_id, CONFIG_KEY_GATEWAY_TOKEN_ENC)
        return
    enc = crypto.encrypt(token)
    await db.g_set_setting(guild_id, CONFIG_KEY_GATEWAY_TOKEN_ENC, enc, is_secret=True)


async def get_server_id(guild_id: int) -> str:
    """Transport B: server_id label. Defaults to 'main' per mod spec.

    v3.1.1: returns lowercased value (mod normalizes to lowercase before
    sending, so we must match)."""
    raw = await db.g_get_setting(guild_id, CONFIG_KEY_SERVER_ID)
    return _normalize_server_id(raw)


async def set_server_id(guild_id: int, server_id: str) -> None:
    """Set transport B server_id label.

    v3.1.1: normalize to lowercase per mod spec. Validate against
    [a-z0-9._-]{1,32} — mod rejects invalid server_id with 400.

    Raises ValueError if server_id doesn't match the spec after lowercasing.
    """
    sid = _normalize_server_id(server_id)
    _validate_server_id(sid)
    await db.g_set_setting(guild_id, CONFIG_KEY_SERVER_ID, sid, is_secret=False)


async def get_last_event_id(guild_id: int) -> int:
    """Cursor: highest event_id we've processed. 0 if none yet."""
    val = await db.g_get_setting(guild_id, CONFIG_KEY_LAST_EVENT_ID)
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


async def set_last_event_id(guild_id: int, event_id: int) -> None:
    await db.g_set_setting(guild_id, CONFIG_KEY_LAST_EVENT_ID, str(int(event_id)), is_secret=False)


async def get_last_command_id(guild_id: int) -> int:
    """Cursor: highest command_id sent to mod (transport B). 0 if none."""
    val = await db.g_get_setting(guild_id, CONFIG_KEY_LAST_COMMAND_ID)
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


async def set_last_command_id(guild_id: int, command_id: int) -> None:
    await db.g_set_setting(guild_id, CONFIG_KEY_LAST_COMMAND_ID, str(int(command_id)), is_secret=False)


# === Events journal ===

async def append_event(guild_id: int, event: dict) -> bool:
    """Insert an event received from mod. Idempotent by event_id (PRIMARY KEY).

    Returns True if newly inserted, False if duplicate (event_id already existed).
    Event shape (from rest_api.md §7):
      {id, ts, type, player_uuid, player_name, data}
    """
    table = _guild.guild_table(guild_id, "vq_events")
    event_id = int(event.get("id") or 0)
    if event_id <= 0:
        return False
    async with _connect() as conn:
        try:
            await conn.execute(
                f"INSERT OR IGNORE INTO {table} "
                f"(event_id, ts, type, player_uuid, player_name, data_json, received_at, processed) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    event_id,
                    int(event.get("ts") or 0),
                    str(event.get("type") or ""),
                    event.get("player_uuid"),
                    event.get("player_name"),
                    json.dumps(event.get("data") or {}, ensure_ascii=False),
                    datetime.utcnow().isoformat(),
                ),
            )
            await conn.commit()
            cur = await conn.execute(f"SELECT changes() FROM {table} LIMIT 1")
            row = await cur.fetchone()
            return bool(row and row[0] > 0)
        except Exception as e:
            log.warning("append_event failed for guild %s event %s: %s", guild_id, event_id, e)
            return False


async def list_events(
    guild_id: int,
    after_id: int = 0,
    limit: int = 100,
    event_type: Optional[str] = None,
) -> list[dict]:
    """List events with id > after_id (newest first or oldest first — caller decides).

    Returns list of dicts (parsed from data_json).
    """
    table = _guild.guild_table(guild_id, "vq_events")
    limit = max(1, min(int(limit) or 100, 1000))
    sql = (f"SELECT event_id, ts, type, player_uuid, player_name, data_json, received_at "
           f"FROM {table} WHERE event_id > ?")
    params: list = [int(after_id)]
    if event_type:
        sql += " AND type = ?"
        params.append(event_type)
    sql += f" ORDER BY event_id ASC LIMIT ?"
    params.append(limit)
    async with _connect() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        try:
            data = json.loads(r[5]) if r[5] else {}
        except Exception:
            data = {}
        result.append({
            "id": r[0], "ts": r[1], "type": r[2],
            "player_uuid": r[3], "player_name": r[4],
            "data": data, "received_at": r[6],
        })
    return result


async def get_max_event_id(guild_id: int) -> int:
    """Highest event_id in the journal (0 if empty)."""
    table = _guild.guild_table(guild_id, "vq_events")
    async with _connect() as conn:
        async with conn.execute(f"SELECT MAX(event_id) FROM {table}") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row and row[0] else 0


# === Commands queue (Transport B) ===

async def enqueue_command(
    guild_id: int,
    method: str,
    path: str,
    query: str = "",
    body: Optional[dict] = None,
    source: str = "panel",
) -> int:
    """Queue a virtual HTTP command for the mod to execute (transport B).
    Returns the new command_id."""
    table = _guild.guild_table(guild_id, "vq_commands")
    method = method.upper()
    if method not in ("GET", "POST", "PATCH", "DELETE"):
        raise ValueError(f"method must be GET/POST/PATCH/DELETE, got {method!r}")
    async with _connect() as conn:
        cur = await conn.execute(
            f"INSERT INTO {table} (method, path, query, body_json, created_at, source) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (
                method, path, query or "",
                json.dumps(body, ensure_ascii=False) if body is not None else None,
                datetime.utcnow().isoformat(), source,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def list_pending_commands(guild_id: int, after_id: int = 0, limit: int = 100) -> list[dict]:
    """List commands with id > after_id that haven't been sent to mod yet
    (status IS NULL). For transport B GET /vq/commands response."""
    table = _guild.guild_table(guild_id, "vq_commands")
    limit = max(1, min(int(limit) or 100, 1000))
    sql = (f"SELECT command_id, method, path, query, body_json "
           f"FROM {table} WHERE command_id > ? AND status IS NULL "
           f"ORDER BY command_id ASC LIMIT ?")
    async with _connect() as conn:
        async with conn.execute(sql, [int(after_id), limit]) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        try:
            body = json.loads(r[4]) if r[4] else None
        except Exception:
            body = None
        cmd = {"id": r[0], "method": r[1], "path": r[2]}
        if r[3]:
            cmd["query"] = r[3]
        if body is not None:
            cmd["body"] = body
        result.append(cmd)
    return result


async def store_command_result(guild_id: int, command_id: int, status: int, body: Any) -> bool:
    """Store the result returned by the mod for a previously-sent command.
    Idempotent by command_id — returns True if newly stored, False if duplicate
    (mod might re-send same result on retries)."""
    table = _guild.guild_table(guild_id, "vq_commands")
    body_str = json.dumps(body, ensure_ascii=False) if body is not None else None
    async with _connect() as conn:
        # Only update if status IS NULL — idempotent dedup
        cur = await conn.execute(
            f"UPDATE {table} SET status = ?, result_json = ?, result_at = ? "
            f"WHERE command_id = ? AND status IS NULL",
            (int(status), body_str, datetime.utcnow().isoformat(), int(command_id)),
        )
        await conn.commit()
        return cur.rowcount > 0


async def mark_command_sent(guild_id: int, command_id: int) -> None:
    """Mark a command as sent to mod (sent_at timestamp)."""
    table = _guild.guild_table(guild_id, "vq_commands")
    async with _connect() as conn:
        await conn.execute(
            f"UPDATE {table} SET sent_at = ? WHERE command_id = ? AND sent_at IS NULL",
            (datetime.utcnow().isoformat(), int(command_id)),
        )
        await conn.commit()


async def get_command(guild_id: int, command_id: int) -> Optional[dict]:
    """Get a single command by id (including status/result if available)."""
    table = _guild.guild_table(guild_id, "vq_commands")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT command_id, method, path, query, body_json, status, result_json, "
            f"created_at, sent_at, result_at, source "
            f"FROM {table} WHERE command_id = ?",
            [int(command_id)],
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        body = json.loads(row[4]) if row[4] else None
    except Exception:
        body = None
    try:
        result = json.loads(row[6]) if row[6] else None
    except Exception:
        result = None
    return {
        "id": row[0], "method": row[1], "path": row[2], "query": row[3] or "",
        "body": body, "status": row[5], "result": result,
        "created_at": row[7], "sent_at": row[8], "result_at": row[9],
        "source": row[10],
    }


async def get_max_command_id(guild_id: int) -> int:
    """Highest command_id ever queued (for cursor reporting to mod)."""
    table = _guild.guild_table(guild_id, "vq_commands")
    async with _connect() as conn:
        async with conn.execute(f"SELECT MAX(command_id) FROM {table}") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row and row[0] else 0


# === Links cache ===

async def upsert_link(guild_id: int, link: dict) -> None:
    """Insert/update link cache entry. link shape:
      {discord_id, discord_username?, mc_uuid, mc_name, linked_at}
    """
    table = _guild.guild_table(guild_id, "vq_links")
    discord_id = int(link.get("discord_id") or 0)
    if discord_id <= 0:
        return
    async with _connect() as conn:
        await conn.execute(
            f"INSERT OR REPLACE INTO {table} "
            f"(discord_id, mc_uuid, mc_name, discord_username, linked_at, cached_at) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (
                discord_id,
                str(link.get("mc_uuid") or ""),
                str(link.get("mc_name") or ""),
                link.get("discord_username"),
                int(link.get("linked_at") or 0),
                datetime.utcnow().isoformat(),
            ),
        )
        await conn.commit()


async def get_link_by_discord(guild_id: int, discord_id: int) -> Optional[dict]:
    """Get link cache entry by Discord ID. Returns None if not linked."""
    table = _guild.guild_table(guild_id, "vq_links")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT discord_id, mc_uuid, mc_name, discord_username, linked_at, cached_at "
            f"FROM {table} WHERE discord_id = ?",
            [int(discord_id)],
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "discord_id": row[0], "mc_uuid": row[1], "mc_name": row[2],
        "discord_username": row[3], "linked_at": row[4], "cached_at": row[5],
    }


async def get_link_by_mc_uuid(guild_id: int, mc_uuid: str) -> Optional[dict]:
    """Get link cache entry by Minecraft UUID."""
    table = _guild.guild_table(guild_id, "vq_links")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT discord_id, mc_uuid, mc_name, discord_username, linked_at, cached_at "
            f"FROM {table} WHERE mc_uuid = ?",
            [str(mc_uuid)],
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "discord_id": row[0], "mc_uuid": row[1], "mc_name": row[2],
        "discord_username": row[3], "linked_at": row[4], "cached_at": row[5],
    }


async def delete_link(guild_id: int, discord_id: int) -> bool:
    """Delete link cache entry. Returns True if deleted."""
    table = _guild.guild_table(guild_id, "vq_links")
    async with _connect() as conn:
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE discord_id = ?",
            [int(discord_id)],
        )
        await conn.commit()
        return cur.rowcount > 0


async def list_links(guild_id: int) -> list[dict]:
    """List all cached links (discord_id ↔ mc_uuid)."""
    table = _guild.guild_table(guild_id, "vq_links")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT discord_id, mc_uuid, mc_name, discord_username, linked_at, cached_at "
            f"FROM {table} ORDER BY linked_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "discord_id": r[0], "mc_uuid": r[1], "mc_name": r[2],
            "discord_username": r[3], "linked_at": r[4], "cached_at": r[5],
        }
        for r in rows
    ]

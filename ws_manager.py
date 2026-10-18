"""
WebSocket manager с изоляцией по guild_id (v2.4.0).

Каждое подключение привязывается к конкретному guild_id после
аутентификации. Broadcast отправляется только клиентам той же гильдии.

События (broadcast по guild_id):
  - wheel_updated: список лотов изменился
  - spin_started: спин начался (с параметрами физики)
  - spin_completed: спин завершён (с winner + final_rotation)
  - collection_*: события сбора фильмов
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import WebSocket

log = logging.getLogger("ws")

# Подключения сгруппированы по guild_id
# guild_id → set of WebSocket
_guild_connections: Dict[int, Set[WebSocket]] = {}
# Обратный индекс: WebSocket → guild_id (для быстрого удаления)
_ws_to_guild: Dict[WebSocket, int] = {}


async def connect(websocket: WebSocket, guild_id: int) -> None:
    """Принять новое подключение и привязать к guild_id."""
    await websocket.accept()
    _guild_connections.setdefault(guild_id, set()).add(websocket)
    _ws_to_guild[websocket] = guild_id
    log.info("WS connected: guild=%d, total in guild=%d, total overall=%d",
             guild_id, len(_guild_connections.get(guild_id, set())),
             sum(len(v) for v in _guild_connections.values()))


async def disconnect(websocket: WebSocket) -> None:
    """Удалить подключение. Идемпотентно — повторный вызов для того же
    websocket не бросает исключение и не пишет в лог повторно.

    v3.0: добавлен guard от повторных вызовов. Раньше при исключении в
    wheel_routes.ws_wheel() могло вызываться дважды (в except-блоке и при
    падении `await websocket.close()`), что порождао два warning'а в логах.
    """
    guild_id = _ws_to_guild.pop(websocket, None)
    if guild_id is None:
        # Уже удалён — silent no-op.
        return
    conns = _guild_connections.get(guild_id)
    if conns:
        conns.discard(websocket)
        if not conns:
            del _guild_connections[guild_id]
    log.info("WS disconnected: guild=%s, remaining=%d",
             guild_id, sum(len(v) for v in _guild_connections.values()))


async def broadcast_guild(guild_id: int, event_type: str, payload: dict) -> None:
    """Разослать событие всем клиентам конкретной гильдии.

    v3.0: каждое исключение пишется на WARNING (раньше было DEBUG —
    реальные проблемы маскировались). Мёртвые сокеты убираются из
    пула даже если вся итерация упала.
    """
    conns = _guild_connections.get(int(guild_id))
    if not conns:
        return
    message = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False)
    dead: list[WebSocket] = []
    for ws in list(conns):
        try:
            await ws.send_text(message)
        except Exception as e:
            # v3.0: WARNING — раньше DEBUG, проблемы были невидимы
            log.warning("WS send to %s failed: %s — will remove from pool", type(ws).__name__, e)
            dead.append(ws)
    for ws in dead:
        conns.discard(ws)
        _ws_to_guild.pop(ws, None)


async def broadcast_all(event_type: str, payload: dict) -> None:
    """Разослать всем (использовать с осторожностью — только для глобальных событий)."""
    for guild_id in list(_guild_connections.keys()):
        await broadcast_guild(guild_id, event_type, payload)


# === Удобные обёртки для событий колеса ===

async def broadcast_wheel_updated(guild_id: int, items: list[dict]) -> None:
    """Список лотов изменился."""
    await broadcast_guild(guild_id, "wheel_updated", {
        "items": items, "count": len(items)
    })


async def broadcast_spin_started(guild_id: int, spin_id: str,
                                  physics: dict, mode: str = "classic",
                                  items: list[dict] | None = None,
                                  duration: int = 5) -> None:
    """Спин начался — клиенты запускают анимацию по физическим параметрам."""
    await broadcast_guild(guild_id, "spin_started", {
        "spin_id": spin_id,
        "physics": physics,
        "mode": mode,
        "items": items or [],
        "duration": duration,
    })


async def broadcast_spin_completed(guild_id: int, spin_id: str,
                                     winner: dict | None, final_rotation: float,
                                     mode: str = "classic",
                                     eliminated: dict | None = None,
                                     remaining_count: int = 0) -> None:
    """Спин завершён — все клиенты синхронизируют финальную позицию."""
    await broadcast_guild(guild_id, "spin_completed", {
        "spin_id": spin_id,
        "winner": winner,
        "final_rotation": final_rotation,
        "mode": mode,
        "eliminated": eliminated,
        "remaining_count": remaining_count,
    })


# === Collection events ===

async def broadcast_collection_started(guild_id: int, collection: dict) -> None:
    await broadcast_guild(guild_id, "collection_started", {"collection": collection})


async def broadcast_collection_participant_joined(guild_id: int, participant: dict) -> None:
    await broadcast_guild(guild_id, "collection_participant_joined", {"participant": participant})


async def broadcast_collection_participant_ready(guild_id: int, participant: dict) -> None:
    await broadcast_guild(guild_id, "collection_participant_ready", {"participant": participant})


async def broadcast_collection_picks_updated(guild_id: int, user_discord_id: int, picks_count: int) -> None:
    await broadcast_guild(guild_id, "collection_picks_updated", {
        "user_discord_id": user_discord_id, "picks_count": picks_count
    })


async def broadcast_collection_participant_kicked(guild_id: int, user_discord_id: int, kicked_by: int) -> None:
    await broadcast_guild(guild_id, "collection_participant_kicked", {
        "user_discord_id": user_discord_id, "kicked_by": kicked_by
    })


async def broadcast_collection_completed(guild_id: int, picks_count: int) -> None:
    await broadcast_guild(guild_id, "collection_completed", {"picks_count": picks_count})


async def broadcast_collection_cancelled(guild_id: int, cancelled_by: int) -> None:
    await broadcast_guild(guild_id, "collection_cancelled", {"cancelled_by": cancelled_by})
